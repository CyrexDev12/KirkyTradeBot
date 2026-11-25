import asyncio
from contextlib import closing
from dataclasses import asdict, replace
from decimal import Decimal
import json
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.updown.feed import Settlement
from backend.updown.strategy import Book, Window
from backend.trading.venue import PublicRequestDeadline
from backend.wti import live_worker
from backend.wti.live_books import BookUnavailable, LiveBooks
from backend.wti.live_store import LiveStore, live_path
from backend.wti.live_venue import AmbiguousOrder, LiveOrder, MarketRules, PublicMarketUnavailable, VenueError
from backend.wti.live_worker import (
    REFRESH_INTERVAL, RULES_MAX_AGE, EntrySizing, Pilot, prepare_entry_contracts, request_priority_mode,
)
from backend.wti.oil_d_shadow_inputs import ShadowInputs, publish_shadow_inputs
from backend.wti.replay_capture import StreamBookSource, current_window
from backend.wti.tape import Tape, tape_path


def d(value):
    return Decimal(value)


START = 9_000
END = START + 900
SLUG = "cpwtif-wti-updown-15m-1970-01-01-0230z"


class Market:
    def __init__(self, clock, store):
        self.clock, self.store = clock, store
        self.sent, self.orders, self.cancelled = [], {}, []
        self.position, self.bid = d("0"), .37
        self.ambiguous = None
        self.settled = False
        self.fill_entry = True

    async def market(self, slug):
        return MarketRules(slug, d("0.01"), d("0.01"), "MARKET_STATUS_OPEN", END)

    async def book(self, slug):
        now = self.clock()
        return Book(((self.bid, 100),), ((.40, 100),), now, now, now)

    async def buying_power(self):
        return d("12.00")

    async def positions(self):
        return {SLUG: self.position} if self.position else {}

    async def open_orders(self):
        return tuple(o for o in self.orders.values() if not o.terminal)

    async def submit_limit(self, *, slug, side, action, quantity, price, key, expires_at):
        saved = self.store.orders(self.store.active_trade()["id"])[-1]
        assert saved["key"] == key and saved["state"] == "sending"
        self.sent.append((action, quantity, price, expires_at))
        order_id = f"order-{len(self.sent)}"
        if action == "buy":
            filled, average, fee = (d("1.20"), d(".40"), d(".02")) if self.fill_entry else (d("0"), None, None)
            state = "ORDER_STATE_CANCELED"
            self.position += filled
        elif expires_at is not None:
            filled, average, fee, state = d("0"), None, None, "ORDER_STATE_NEW"
        else:
            filled, average, fee, state = quantity, d(".18"), d(".01"), "ORDER_STATE_FILLED"
            self.position -= filled
        reply = LiveOrder(order_id, slug, side, action, quantity, filled, quantity - filled, price, average, state, fee)
        self.orders[order_id] = reply
        if self.ambiguous:
            error, self.ambiguous = self.ambiguous, None
            raise AmbiguousOrder("Unknown submission", order_id if error == "id" else None)
        return reply

    async def order(self, order_id):
        return self.orders[order_id]

    async def cancel(self, order_id, slug):
        self.cancelled.append(order_id)

    async def settlement(self, product, start):
        return Settlement(SLUG, 1, 80, 81) if self.settled else None


def scenario(directory, *, stream_snapshot=None, now=None, **pilot_options):
    now = [START + 100.0] if now is None else now
    clock = lambda: now[0]
    live = LiveStore(live_path(directory))
    market = Market(clock, live)
    pilot = Pilot(live, market, market, directory, d("12"), clock, stream_snapshot=stream_snapshot, **pilot_options)
    tape = Tape(tape_path(directory))
    tape.record_window(Window("wti", SLUG, START, END, 80, "MARKET_STATUS_OPEN", .095, rule="point"))
    info = {"action": "buy", "side": "UP", "quantity": 15, "limit": .4, "budget": 5,
            "ready_at": now[0] + 3, "deadline": END - 90, "theta": .095}
    tape.append(SLUG, now[0], "intent", "order", {**info, "model": "E"})
    tape.append(SLUG, now[0] + 1, "intent", "order", {**info, "model": "D"})
    return now, live, market, pilot


def test_oil_d_only_caps_entry_and_handles_take_profit_fill_during_stop_cancel(tmp_path):
    now, live, market, pilot = scenario(tmp_path)

    async def run():
        await pilot.tick()
        assert not market.sent
        now[0] += 3
        await pilot.tick()
        assert market.sent == [("buy", d("2.27"), d(".40"), None)]
        await pilot.tick()
        assert market.sent[-1] == ("sell", d("1.20"), d(".70"), END - 5)

        live.disable(now[0], "operator")
        market.bid = .18
        now[0] += 3.1
        await pilot.tick()
        assert market.cancelled == ["order-2"] and len(market.sent) == 2
        market.orders["order-2"] = replace(market.orders["order-2"], state="ORDER_STATE_CANCELED",
                                           filled=d(".20"), remaining=d("1.00"), average_price=d(".70"), fees=d("0"))
        market.position -= d(".20")
        now[0] += 1
        await pilot.tick()
        assert market.sent[-1] == ("sell", d("1.00"), d(".16"), None)
        await pilot.tick()
        assert live.active_trade() is None and live.realized() == d("-.19")
        assert not live.enabled() and market.position == d("0")

    asyncio.run(run())


def test_working_order_at_cutoff_cancels_during_public_cooldown(tmp_path):
    now, live, market, pilot = scenario(tmp_path)

    async def unavailable(slug):
        raise PublicRequestDeadline()

    async def run():
        assert request_priority_mode(live, True) == (True, False)
        now[0] += 3
        await pilot.tick()
        await pilot.tick()
        assert market.sent[-1] == ("sell", d("1.20"), d(".70"), END - 5)
        live.disable(now[0], "operator")
        assert request_priority_mode(live, True) == (False, True)
        now[0] = END - 5
        market.market = unavailable
        await pilot.tick()
        assert market.cancelled == ["order-2"]

    asyncio.run(run())


def test_preverified_rules_allow_entry_during_public_cooldown_using_fresh_rest_book(tmp_path):
    now, live, market, pilot = scenario(tmp_path)

    async def unavailable(slug):
        raise AssertionError("The prepared entry should not call the public gateway")

    async def run():
        stop = asyncio.Event()
        verify = market.market

        async def verify_once(slug):
            stop.set()
            return await verify(slug)

        market.market = verify_once
        await asyncio.wait_for(prepare_entry_contracts(pilot, stop), timeout=1)
        assert pilot.entry_markets.status() == {"slug": SLUG, "verified_at": now[0]}
        market.market = market.book = unavailable
        now[0] += 3
        book = Book(((.37, 100),), ((.40, 100),), now[0], now[0], now[0])
        Tape(pilot.tape).append(SLUG, now[0], "rest", "book", asdict(book))
        await pilot.tick()
        assert market.sent == [("buy", d("2.27"), d(".40"), None)]

    asyncio.run(run())


@pytest.mark.parametrize("stale_exchange,early_request", [(True, False), (False, True)])
def test_preverified_rules_still_require_fresh_post_signal_rest_book(tmp_path, stale_exchange, early_request):
    now, live, market, pilot = scenario(tmp_path)

    async def run():
        await pilot.entry_markets.prime(current_window(pilot.tape, now[0]))
        now[0] += 8.7
        quote = await market.book(SLUG)
        Tape(pilot.tape).append(SLUG, now[0], "stream", "book", asdict(quote))

        async def bad_book(slug):
            return replace(quote, exchange_at=now[0] - 16 if stale_exchange else now[0],
                           requested_at=now[0] - 6 if early_request else now[0])

        market.book = bad_book
        await pilot.tick()
        assert "freshness" in pilot.report["message"]
        assert not market.sent

    asyncio.run(run())


def test_oil_d_checks_account_and_book_concurrently_and_persists_timing(tmp_path):
    now, live, market, pilot = scenario(tmp_path)
    fetch_book = market.book

    async def run():
        checking_account = asyncio.Event()
        checking_book = asyncio.Event()

        async def positions():
            checking_account.set()
            await asyncio.wait_for(checking_book.wait(), timeout=.75)
            return {}

        async def book(slug):
            checking_book.set()
            await asyncio.wait_for(checking_account.wait(), timeout=.75)
            snapshot = await fetch_book(slug)
            return replace(snapshot, exchange_at=now[0] - 10.374)

        market.positions, market.book = positions, book
        now[0] += 3
        await pilot.tick()
        assert len(market.sent) == 1
        with closing(live._connect()) as db:
            data = json.loads(db.execute("SELECT payload FROM entry_timings").fetchone()[0])
        assert data["book_search_started_at"] <= data["checks_completed_at"] <= data["submit_started_at"]
        assert data["outcome"] == "submitted" and data["acknowledged"]
        assert data["max_exchange_age_seconds"] == 15
        assert data["filled"] == "1.20"
        assert any(q["stage"] == "pre_submit" and q["reason"] == "usable" for q in data["quotes"])
        assert not any(event["kind"] == "entry_timing" for event in live.events()["events"])

    asyncio.run(run())


def test_newly_closed_book_vetoes_entry_after_journal_authorization(tmp_path):
    now, live, market, pilot = scenario(tmp_path)
    authorize = live.authorize_entry
    tape = Tape(pilot.tape)

    def authorize_then_close(order_id, at):
        allowed = authorize(order_id, at)
        tape.append(SLUG, now[0], "rest", "closed", {})
        return allowed

    live.authorize_entry = authorize_then_close
    now[0] += 3
    asyncio.run(pilot.tick())
    assert not market.sent
    assert live.orders(live.active_trade()["id"])[0]["state"] == "ORDER_STATE_CANCELED"


def test_stream_first_waits_for_depth_and_uses_fresh_stream_for_exit(tmp_path, monkeypatch):
    latest = [None]
    now, live, market, pilot = scenario(tmp_path, stream_snapshot=lambda slug: (latest[0], False))
    assess = EntrySizing.assess

    async def run():
        rejected = asyncio.Event()

        def watch(self, book):
            reason, details = assess(self, book)
            if reason == "price_above_limit":
                rejected.set()
            return reason, details

        async def unexpected_rest(slug):
            raise AssertionError("A fresh stream quote was available")

        monkeypatch.setattr(EntrySizing, "assess", watch)
        market.book = unexpected_rest
        now[0] += 3
        latest[0] = Book(((.37, 100),), ((.41, 100),), now[0], now[0])
        task = asyncio.create_task(pilot.tick())
        try:
            await asyncio.wait_for(rejected.wait(), timeout=1)
            assert not market.sent and live.active_trade() is None
            now[0] += .25
            latest[0] = Book(((.37, 100),), ((.40, 100),), now[0], now[0])
            await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert market.sent == [("buy", d("2.27"), d(".40"), None)]
        with closing(live._connect()) as db:
            data = json.loads(db.execute("SELECT payload FROM entry_timings").fetchone()[0])
        assert data["final_quote_source"] == "stream" and data["book_mode"] == "stream_first"
        assert any(q["reason"] == "price_above_limit" and q["best_ask"] == "0.41" for q in data["quotes"])

        await pilot.tick()
        assert market.sent[-1] == ("sell", d("1.20"), d(".70"), END - 5)

    asyncio.run(run())


def test_stream_halt_during_private_exit_checks_prevents_an_order(tmp_path):
    latest = [(None, False)]
    now, live, market, pilot = scenario(tmp_path, stream_snapshot=lambda slug: latest[0])

    async def run():
        now[0] += 3
        latest[0] = (Book(((.37, 100),), ((.40, 100),), now[0], now[0]), False)
        await pilot.tick()
        positions = market.positions

        async def positions_after_halt():
            latest[0] = (None, True)
            return await positions()

        market.positions = positions_after_halt
        with pytest.raises(BookUnavailable, match="not open"):
            await pilot.tick()
        assert [order[0] for order in market.sent] == ["buy"]
        assert [order["kind"] for order in live.orders(live.active_trade()["id"])] == ["entry"]

    asyncio.run(run())


def native_stream_scenario(directory, *, watch=None, **options):
    now = [START + 100.0]
    source = StreamBookSource(lambda: now[0])

    def snapshot(slug):
        quote, halted = source.snapshot(slug)
        if watch is not None:
            watch(quote, now[0])
        return quote, halted

    now, live, market, pilot = scenario(
        directory, now=now, stream_snapshot=snapshot, book_mode="stream_end_to_end", **options,
    )
    tape = Tape(pilot.tape)
    window = Window("wti", SLUG, START, END, 80, "MARKET_STATUS_OPEN", .095, rule="point",
                    minimum_quantity="0.01", price_tick="0.01")
    tape.record_window(window)
    publish_shadow_inputs(directory, ShadowInputs(now[0], now[0] - 1, 81, (80,) * 121, None, None))
    generation = source.begin(window)
    source.set_connected(generation, True)

    def frame(ask, sequence):
        book = Book(((.37, 100),), ((ask, 100),), now[0], now[0], sequence=sequence)
        source.observe(generation, (SLUG, book.at, "stream", "book", asdict(book)))

    return now, live, market, pilot, tape, frame


def test_stream_end_to_end_generates_its_own_signal_and_uses_the_later_book_at_original_limit(tmp_path, monkeypatch):
    async def run():
        early_seen, price_rejected = asyncio.Event(), asyncio.Event()

        def watch(book, now):
            if book is not None and book.sequence == 2 and now >= START + 102:
                early_seen.set()

        now, live, market, pilot, tape, frame = native_stream_scenario(tmp_path, watch=watch)
        signal_at = now[0]
        tape.append(SLUG, signal_at + .25, "intent", "order", {
            "model": "D", "action": "buy", "side": "DOWN", "quantity": 5, "limit": .60, "budget": 4,
            "ready_at": signal_at + 2, "deadline": END - 90, "theta": .095,
        })
        simulate = live_worker.simulate_arrival

        def observe_price(intent, book, **kwargs):
            result = simulate(intent, book, **kwargs)
            if result.reason == "price_above_limit" and book.sequence == 3:
                price_rejected.set()
            return result

        def no_rest_tape(*_):
            raise AssertionError("The end-to-end entry consulted a REST tape book")

        async def no_rest_book(_):
            raise AssertionError("The end-to-end entry tried a REST order-book GET")

        monkeypatch.setattr(live_worker, "simulate_arrival", observe_price)
        monkeypatch.setattr(LiveBooks, "_recorded", no_rest_tape)
        market.book = no_rest_book
        frame(.40, 1)
        await asyncio.wait_for(pilot.tick(), timeout=2)
        assert not market.sent and live.active_trade() is None

        now[0] = signal_at + 1.75
        frame(.39, 2)
        await asyncio.wait_for(pilot.tick(), timeout=2)
        assert not market.sent

        now[0] = signal_at + 2
        task = asyncio.create_task(pilot.tick())
        try:
            await asyncio.wait_for(early_seen.wait(), timeout=2)
            assert not market.sent
            now[0] += .25
            frame(.41, 3)
            await asyncio.wait_for(price_rejected.wait(), timeout=2)
            assert not market.sent
            now[0] += .25
            frame(.39, 4)
            await asyncio.wait_for(task, timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert len(market.sent) == 1
        action, quantity, limit, expiration = market.sent[0]
        assert (action, limit, expiration) == ("buy", d(".40"), None)
        assert quantity % d(".01") == 0
        assert quantity * (limit + d(".03")) + d(".02") <= d("1")
        order = live.orders(live.active_trade()["id"])[0]
        assert order["request"]["side"] == "UP" and order["request"]["signal_at"] == signal_at
        assert order["venue_id"] == "order-1" and d(order["payload"]["filled"]) > 0
        with closing(live._connect()) as db:
            data = json.loads(db.execute("SELECT payload FROM entry_timings").fetchone()[0])
        assert data["signal_source"] == data["final_quote_source"] == "stream"
        assert data["book_mode"] == "stream_end_to_end" and data["ready_at"] == signal_at + 2
        assert data["final_book_received_at"] == signal_at + 2.5
        assert any(q["reason"] in ("request_before_ready", "frame_before_ready") for q in data["quotes"])
        assert any(q["reason"] == "price_above_limit" for q in data["quotes"])

    asyncio.run(run())


@pytest.mark.parametrize("blocked", ["configuration", "journal", "rest_only"])
def test_stream_end_to_end_cannot_open_when_paused_or_when_only_rest_has_quotes(tmp_path, blocked):
    now, live, market, pilot, tape, frame = native_stream_scenario(
        tmp_path, entries_allowed=blocked != "configuration",
    )
    if blocked == "journal":
        live.disable(now[0], "New entries stopped by operator")

    async def no_rest_book(_):
        raise AssertionError("The end-to-end mode attempted a REST order-book GET")

    market.book = no_rest_book

    async def run():
        for seq in (1, 2, 3):
            if blocked == "rest_only":
                good = Book(((.37, 100),), ((.40, 100),), now[0], now[0], now[0])
                tape.append(SLUG, now[0], "rest", "book", asdict(good))
            else:
                frame(.40, seq)
            await asyncio.wait_for(pilot.tick(), timeout=2)
            now[0] += 3
        assert not market.sent and live.active_trade() is None
        with closing(live._connect()) as db:
            assert db.execute("SELECT count(*) FROM signals").fetchone()[0] == 0

    asyncio.run(run())


def test_stream_end_to_end_checks_exchange_exposure_before_submitting(tmp_path):
    now, live, market, pilot, _, frame = native_stream_scenario(tmp_path)

    async def run():
        frame(.40, 1)
        await pilot.tick()
        now[0] += 2
        frame(.39, 2)
        market.position = d("1")
        await pilot.tick()
        assert not market.sent and live.active_trade() is None
        with closing(live._connect()) as db:
            data = json.loads(db.execute("SELECT payload FROM entry_timings").fetchone()[0])
        assert (data["signal_source"], data["outcome"], data["reason"]) == (
            "stream", "skipped", "account_has_exposure",
        )

    asyncio.run(run())


def test_primer_retains_only_recent_matching_rules_during_transient_failure(tmp_path):
    now, live, market, pilot = scenario(tmp_path)
    seeded_market = market.market

    async def temporary(slug):
        raise PublicMarketUnavailable("429")

    async def closed(slug):
        return replace(await seeded_market(slug), status="MARKET_STATUS_CLOSED")

    async def run():
        window = current_window(pilot.tape, now[0])
        await pilot.entry_markets.prime(window)
        now[0] += REFRESH_INTERVAL + 1
        market.market = temporary
        with pytest.raises(PublicMarketUnavailable):
            await pilot.entry_markets.prime(window)
        assert pilot.entry_markets.verified(SLUG, END) is not None
        assert pilot.entry_markets.verified(SLUG, END + 900) is None

        now[0] += RULES_MAX_AGE - REFRESH_INTERVAL
        assert pilot.entry_markets.verified(SLUG, END) is None
        market.market = seeded_market
        await pilot.entry_markets.prime(window)
        assert pilot.entry_markets.verified(SLUG, END) is not None

        now[0] += REFRESH_INTERVAL + 1
        market.market = closed
        with pytest.raises(VenueError, match="as open"):
            await pilot.entry_markets.prime(window)
        assert pilot.entry_markets.verified(SLUG, END) is None

    asyncio.run(run())


def test_cold_market_http_wait_cannot_outlast_entry_signal(tmp_path):
    now, live, market, pilot = scenario(tmp_path)

    async def stalled(slug):
        await asyncio.Event().wait()

    market.market = stalled
    now[0] += 8.99  # Only 10 ms of the signal's entry window remain.
    asyncio.run(pilot.tick())
    assert "expired while checking account and contract status" in pilot.report["message"]
    assert not market.sent


def test_live_book_http_wait_cannot_outlast_entry_signal(tmp_path):
    now, live, market, pilot = scenario(tmp_path)
    called = []

    async def stalled(slug):
        called.append(slug)
        await asyncio.Event().wait()

    async def run():
        await pilot.entry_markets.prime(current_window(pilot.tape, now[0]))
        market.book = stalled
        now[0] += 8.80
        await pilot.tick()
        assert called == [SLUG]
        assert "expired while waiting for a fresh WTI book" in pilot.report["message"]
        assert not market.sent

    asyncio.run(run())


@pytest.mark.parametrize("known_id", (True, False), ids=("can-query-order", "no-exchange-order-id"))
def test_uncertain_submission_disarms_entries_and_never_duplicates(tmp_path, known_id):
    now, live, market, pilot = scenario(tmp_path)
    market.ambiguous = "id" if known_id else "no id"

    async def run():
        now[0] += 3
        with pytest.raises(AmbiguousOrder):
            await pilot.tick()
        assert len(market.sent) == 1 and not live.enabled()
        resumed = Pilot(LiveStore(live.path), market, market, tmp_path, d("12"), lambda: now[0])
        await resumed.tick()
        if known_id:
            assert market.sent[-1] == ("sell", d("1.20"), d(".70"), END - 5)
        else:
            assert len(market.sent) == 1 and resumed.report["state"] == "blocked"

    asyncio.run(run())


def test_position_waits_for_official_settlement_after_end(tmp_path):
    now, live, market, pilot = scenario(tmp_path)

    async def run():
        now[0] += 3
        await pilot.tick()
        now[0] = END + 1
        await pilot.tick()
        assert live.active_trade() and pilot.report["state"] == "settling"
        market.settled = True
        await pilot.tick()
        assert live.active_trade() is None and live.realized() == d(".70")
        assert len(market.sent) == 1

    asyncio.run(run())


def test_live_entry_skips_a_nonempty_exchange_account_and_expires(tmp_path):
    now, live, market, pilot = scenario(tmp_path)

    async def run():
        now[0] += 3
        market.position = d("2")
        await pilot.tick()
        assert pilot.report["state"] == "blocked"
        market.position = d("0")
        now[0] += 7
        await pilot.tick()
        assert live.active_trade() is None and not market.sent

    asyncio.run(run())


def test_rate_limited_entry_posts_one_skip_with_reason_after_expiry(tmp_path):
    now, live, market, pilot = scenario(tmp_path)
    calls = []

    async def unavailable(slug):
        calls.append(slug)
        response = httpx.Response(429, headers={"Retry-After": "60"},
                                  request=httpx.Request("GET", "https://example.invalid/book"))
        response.raise_for_status()

    market.book = unavailable

    async def run():
        now[0] += 8.7
        await pilot.tick()
        assert not [e for e in live.events()["events"] if e["kind"] == "entry_skipped"]
        now[0] += 1
        await pilot.tick()
        await pilot.tick()
        skipped = [e for e in live.events()["events"] if e["kind"] == "entry_skipped"]
        assert len(skipped) == 1 and skipped[0]["side"] == "UP"
        assert "HTTP 429" in live.status()["last_missed_entry"]["message"]
        assert calls == [SLUG] and not market.sent

    asyncio.run(run())


def test_terminal_zero_fill_explains_ioc_outcome_once(tmp_path):
    now, live, market, pilot = scenario(tmp_path)
    market.fill_entry = False

    async def run():
        now[0] += 3
        await pilot.tick()
        await pilot.tick()
        now[0] += 8
        await pilot.tick()
        outcomes = [e for e in live.events()["events"] if e["kind"].startswith("entry_")]
        assert [e["kind"] for e in outcomes] == ["entry_unfilled"]
        assert "zero fills at the limit" in outcomes[0]["message"]
        assert live.active_trade() is None and len(market.sent) == 1

    asyncio.run(run())


@pytest.mark.parametrize("reason,allowed", [("New entries stopped by operator.", True),
                                           ("Exchange order submission could not be confirmed", False),
                                           ("Pilot loss limit reached", False)])
def test_resume_does_not_override_automatic_pauses(tmp_path, reason, allowed):
    live = LiveStore(live_path(tmp_path))
    live.disable(99, reason)
    live.heartbeat(100, {"worker_configured": True, "entries_configured": True})
    assert live.resume_operator_pause(101) is allowed
    assert live.enabled() is allowed


def test_status_and_off_require_operator_and_preserve_pause(tmp_path, monkeypatch):
    from backend.app import app

    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NFL_PAPER_OPERATOR_TOKEN", "local-test-token")
    monkeypatch.delenv("NFL_PAPER_OPERATOR_TOKEN_FILE", raising=False)
    path = live_path(tmp_path / "wti")
    store = LiveStore(path)
    now = time.time()
    store.heartbeat(now, {"worker_configured": True, "entries_configured": True, "state": "waiting"})
    skipped = "Polymarket US rate-limited the order book (HTTP 429)."
    store.event("entry_skipped", now, {"title": "Entry skipped", "message": skipped})
    with closing(TestClient(app)) as client:
        root = "/api/wti/live"
        headers = {"X-Paper-Operator-Token": "local-test-token"}
        assert client.get(f"{root}/status").status_code == 403
        assert client.get(f"{root}/events").status_code == 403
        assert client.post(f"{root}/off", json={"actor": "test"}).status_code == 403
        assert client.get(f"{root}/status", headers=headers).json()["enabled"]
        assert client.post(f"{root}/off", headers=headers, json={"actor": "test"}).json()["paused"]
        assert not LiveStore(path).enabled()
        result = client.get(f"{root}/events?after=0&limit=25", headers=headers)
        assert result.status_code == 200
        events = result.json()["events"]
        assert [event["kind"] for event in events] == ["entry_skipped", "paused"]
        assert events[0]["message"] == skipped
        assert client.get("/api/wti/D/events?after=0&limit=25").status_code == 200
