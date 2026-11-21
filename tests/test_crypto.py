import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from backend.crypto import api
from backend.crypto.market import Bar, DataUnavailable, HISTORY_BARS, Market, Quote, closed_bars
from backend.crypto.store import Store
from backend.crypto.strategy import current_slot, paper_entry, trend
from backend.crypto.worker import Worker

UTC = timezone.utc
SLOT = datetime(2026, 11, 20, 13, tzinfo=UTC)  # 06:00 Arizona


def candles(slot=SLOT, drift=0.001, width=0.009):
    bars = []
    price = 90_000.0
    for i in range(HISTORY_BARS):
        end = price * (1 + drift)
        bars.append(Bar(int(slot.timestamp()) - (HISTORY_BARS - i) * 300, min(price, end) * (1 - width),
                        max(price, end) * (1 + width), price, end))
        price = end
    return bars


def tape(slot=SLOT, direction=1, width=0.006):
    bars = candles(slot, direction * 0.003, width)
    spot = bars[-1].close
    return bars, Quote(spot * 0.99998, spot * 1.00002, slot)


class FakeMarket:
    def __init__(self):
        self.bars_called = []
        self.current_quote = None

    async def bars(self, slot):
        self.bars_called.append(slot)
        bars, self.current_quote = tape(slot)
        return bars

    async def quote(self):
        return self.current_quote


def test_coinbase_reads_only_closed_aligned_complete_buckets():
    original = candles()
    rows = [[b.start, b.low, b.high, b.open, b.close, 1] for b in original]
    future = [int(SLOT.timestamp()), 1, 1e20, 1, 1, 0]
    assert closed_bars([future, *reversed(rows)], SLOT) == original
    for invalid in (rows[1:], rows + [rows[0]], [[*rows[0][:1], float("nan"), *rows[0][2:]], *rows[1:]]):
        with pytest.raises(DataUnavailable):
            closed_bars(invalid, SLOT)

    def handler(request):
        assert request.method == "GET" and request.url.host == "api.exchange.coinbase.com"
        if request.url.path.endswith("/candles"):
            assert request.url.params["granularity"] == "300"
            return httpx.Response(200, json=rows)
        assert request.url.path.endswith("/BTC-USD/ticker")
        return httpx.Response(200, json={"time": SLOT.isoformat(), "bid": "100", "ask": "101"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            market = Market(client)
            assert await market.bars(SLOT) == original
            quote = await market.quote()
            quote.validate(SLOT)
            with pytest.raises(DataUnavailable):
                quote.validate(SLOT + timedelta(seconds=21))
    asyncio.run(run())


@pytest.mark.parametrize("month", [1, 7])
def test_phoenix_schedule_including_dst_and_excluded_0900(month):
    first = SLOT.replace(month=month)
    assert current_slot(first - timedelta(seconds=1)) is None
    slots = []
    for i in range(12):
        at = first + timedelta(minutes=15*i)
        if current_slot(at) == at:
            slots.append(i)
    assert slots == [0, 2, 4, 6, 8, 10]
    assert current_slot(first + timedelta(hours=3)) is None


@pytest.mark.parametrize("side", [1, -1])
def test_conservative_signal_risk_budget_and_transaction_costs(side):
    bars, quote = tape(direction=side)
    s = trend(bars)
    assert s.direction == ("UP" if side == 1 else "DOWN")
    entry, reason = paper_entry(s, quote, 100)
    assert entry is not None, reason
    assert 0 < entry["risk_at_stop"] <= 0.5
    assert entry["target_net_profit"] == pytest.approx(1.5 * entry["risk_at_stop"])
    assert (entry["entry_price"] - (quote.ask if side == 1 else quote.bid)) * side > 0
    assert entry["quantity"] * entry["entry_price"] + entry["entry_fee"] <= 20
    smaller, _ = paper_entry(replace(s, confidence=s.confidence / 2), quote, 100)
    assert 0 < smaller["quantity"] < entry["quantity"]
    lowvol = candles(drift=0.00025 * side, width=0.0004)
    low_signal = trend(lowvol)
    assert low_signal.direction == s.direction
    price = lowvol[-1].close
    assert "exceeds" in paper_entry(low_signal, Quote(price-0.01, price+0.01, SLOT), 100)[1]
    assert trend(candles(drift=0)).direction is None


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("result", ["stop_loss", "take_profit", "window_end"])
def test_lifecycle_is_idempotent_and_separate_from_nfl(tmp_path, side, result):
    db = tmp_path / "crypto" / "paper.sqlite3"
    nfl = tmp_path / "trading" / "paper.sqlite3"
    nfl.parent.mkdir()
    nfl.write_bytes(b"Independent NFL ledger")
    store = Store(db)
    bars, quote = tape(direction=side)
    s = trend(bars)
    def enter(_):
        store.decide(SLOT, SLOT, None, s, quote)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(enter, range(2)))
    reopened = Store(db)
    opened = reopened.open_trade()
    assert opened["direction"] == s.direction and reopened.account()["available_cash"] < 100
    price = opened["stop_loss"] if result == "stop_loss" else opened["take_profit"] if result == "take_profit" else opened["entry_price"]
    at = SLOT + timedelta(minutes=18 if result == "window_end" else 1)
    exit_quote = Quote(price, price, at)
    for _ in range(2):
        reopened.observe(opened["slot_at"], exit_quote, at)
    account = reopened.account()
    assert account["closed_trades"] == 1 and account["open_trade"] is None
    assert account["available_cash"] == account["balance"]
    trade = reopened.trades(10)[0]
    assert trade["exit_reason"] == result and trade["closed_at"] == at.isoformat()
    assert account["balance"] == pytest.approx(100 + (trade["exit_price"] - opened["entry_price"]) *
                                               side * opened["quantity"] - opened["entry_fee"] - trade["exit_fee"])
    events = reopened.events(0, 100)["events"]
    assert [e["kind"] for e in events] == ["opened", "closed"]
    assert events[0]["trade"]["status"] == "OPEN" and "exit_price" not in events[0]["trade"]
    assert reopened.events(events[0]["id"], 100)["events"] == events[1:]
    assert nfl.read_bytes() == b"Independent NFL ledger"


def test_worker_skips_alternating_window_and_recovers_outage_without_backfill(tmp_path):
    now = SLOT
    store = Store(tmp_path / "paper.sqlite3")
    market = FakeMarket()
    w = Worker(store, market, lambda: now)
    asyncio.run(w.tick())
    opened = store.open_trade()
    assert opened is not None
    now += timedelta(minutes=15)
    w = Worker(store, market, lambda: now)  # restarted, no memory of the first order
    asyncio.run(w.tick())
    assert store.open_trade() is not None and w.data_error  # stale quote cannot settle
    # A recent quote from just before expiry is still too early for a timed exit.
    pre_expiry = replace(market.current_quote, at=now - timedelta(seconds=1))
    store.observe(opened["slot_at"], pre_expiry, now)
    assert store.open_trade() is not None
    now += timedelta(minutes=3)
    market.current_quote = replace(market.current_quote, at=now)
    asyncio.run(w.tick())
    assert store.trades(1)[0]["closed_at"] == now.isoformat()
    assert market.bars_called == [SLOT]
    now = SLOT + timedelta(minutes=31)
    asyncio.run(w.tick())
    assert market.bars_called == [SLOT]  # no late entry
    assert store.trades(1)[0]["status"] == "SKIPPED"
    now = SLOT + timedelta(hours=1)
    asyncio.run(w.tick())
    assert market.bars_called == [SLOT, now]


def test_crypto_api_reads_isolated_worker_state(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    assert client.get("/api/crypto/account").json()["balance"] == 100
    api.store().decide(SLOT, SLOT, "test skip")
    (tmp_path / "crypto/health.json").write_text(json.dumps({"last_error": None}))
    assert client.get("/api/crypto/trades").json()["trades"][0]["status"] == "SKIPPED"
    data = client.get("/api/crypto/account").json()
    assert data["worker"]["last_error"] is None and data["latest_id"] == 1
    assert client.get("/api/crypto/events?after=1").json() == {"latest_id": 1, "events": []}
    stats = client.get("/api/crypto/stats").json()
    assert stats["win_rate"] is None and stats["skipped"] == 1


def seed_completed(db, at, result, *, direction="UP", settled=None):
    # A journal entry written by the old release, with no attached learning or record.
    trade = {"slot_at": at.isoformat(), "direction": direction, "risk_at_stop": 0.25,
             "closed_at": (settled or (at + timedelta(minutes=15))).isoformat(), "net_pnl": result}
    db.execute("INSERT INTO decisions VALUES(?, 'CLOSED', ?, ?, ?)",
               (trade["slot_at"], result, at.date().isoformat(), json.dumps(trade)))


def test_stats_and_learning_use_only_prior_settlements_in_matching_setup(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    bars, quote = tape()
    signal = trend(bars)
    with store.transaction(True) as db:
        for day in range(1, 20):
            seed_completed(db, SLOT - timedelta(days=day), -0.25)
        # Three outcomes count toward the overall record but not this setup at entry.
        seed_completed(db, SLOT - timedelta(days=91), 1.00)
        seed_completed(db, SLOT - timedelta(days=20), 0, direction="DOWN")
        seed_completed(db, SLOT - timedelta(days=22) + timedelta(minutes=30), -0.25,
                       settled=SLOT + timedelta(minutes=1))
    store.decide(SLOT - timedelta(days=2, minutes=30), SLOT, "test skip")
    stats = store.stats(SLOT)
    assert (stats["wins"], stats["flats"], stats["losses"], stats["closed_trades"]) == (1, 1, 20, 22)
    assert stats["win_rate"] == pytest.approx(1 / 22)
    assert stats["skipped"] == 1
    setup = next(p for p in stats["patterns"] if p["label"].startswith("Up"))
    assert setup["closed_trades"] == 19 and setup["risk_multiplier"] == 1

    with store.transaction(True) as db:
        seed_completed(db, SLOT - timedelta(days=21), -0.25)
    # Record and apply the decision inside the same transaction, surviving restarts.
    store = Store(store.path)
    normal, _ = paper_entry(signal, quote, store.account()["balance"])
    store.decide(SLOT, SLOT, None, signal, quote)
    opened = store.open_trade()
    assert opened["learning"]["closed_trades"] == 20
    assert opened["learning"]["risk_multiplier"] == 0.5
    assert opened["quantity"] == pytest.approx(normal["quantity"] * 0.5, abs=1e-8)
    assert store.stats(SLOT)["open_trades"] == 1

    later = SLOT + timedelta(minutes=15)
    store.observe(SLOT.isoformat(), Quote(quote.bid, quote.ask, later), later)
    store.observe(SLOT.isoformat(), Quote(quote.bid, quote.ask, later), later)
    last_event = store.events(0, 50)["events"][-1]
    assert last_event["trade"]["record"]["closed_trades"] == 24
    assert last_event["trade"]["record"]["win_rate"] == pytest.approx(1 / 24)


def test_learning_recovers_and_ignores_isolated_drawdown(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    with store.transaction(True) as db:
        for day in range(1, 61):
            seed_completed(db, SLOT - timedelta(days=day), 0.5 if day <= 12 else -0.25)
    pattern = store.stats(SLOT)["patterns"][0]
    assert pattern["closed_trades"] == 40 and pattern["win_rate"] == 0.3
    assert pattern["risk_multiplier"] == 1  # Recent half profitable despite long-run losing record.
    assert store.stats(SLOT + timedelta(days=92))["patterns"] == []


def test_delayed_fill_counts_in_record_but_cannot_train_a_15_minute_pattern(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    with store.transaction(True) as db:
        seed_completed(db, SLOT, -0.25, settled=SLOT + timedelta(hours=1))
    stats = store.stats(SLOT + timedelta(hours=2))
    assert stats["closed_trades"] == stats["losses"] == 1
    assert stats["patterns"] == []


def test_spread_and_daily_loss_limits_survive_restart(tmp_path):
    bars, quote = tape()
    signal = trend(bars)
    store = Store(tmp_path / "crypto.sqlite3")
    wide = replace(quote, ask=quote.ask * 1.01)
    store.decide(SLOT, SLOT, None, signal, wide)
    assert "spread" in store.trades(1)[0]["reason"]
    slot = SLOT + timedelta(minutes=30)
    quote = replace(quote, at=slot)
    store.decide(slot, slot, None, signal, quote)
    at = slot + timedelta(minutes=2)
    gap_price = quote.bid * 0.5
    store.observe(slot.isoformat(), Quote(gap_price, gap_price, at), at)
    reopened = Store(store.path)
    assert reopened.account()["realized_pnl"] < -1.5
    following = slot + timedelta(minutes=30)
    reopened.decide(following, following, None, signal, replace(quote, at=following))
    assert reopened.trades(1)[0]["reason"] == "daily realized loss limit"
    tomorrow = SLOT + timedelta(days=1)
    reopened.decide(tomorrow, tomorrow, None, signal, replace(quote, at=tomorrow))
    assert reopened.open_trade()["slot_at"] == tomorrow.isoformat()
