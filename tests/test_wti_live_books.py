import asyncio
from dataclasses import asdict, replace
import time

import httpx
import pytest

from backend.updown.feed import FeedError
from backend.updown.strategy import Book
from backend.wti import live_books
from backend.wti.live_books import BookUnavailable, LiveBooks
from backend.wti.tape import Tape


SLUG = "wti-window"


def quote(at, exchange_at=None, requested_at=None):
    return Book(((.39, 20),), ((.40, 20),), at, exchange_at, requested_at)


def record(tape, book):
    tape.append(SLUG, book.at, "rest", "book", asdict(book))


def test_uses_shared_quote_only_when_fresh_and_requested_after_eligibility(tmp_path):
    clock = lambda: 107.0
    tape = Tape(tmp_path / "tape.sqlite3")
    calls = []

    class Direct:
        async def book(self, slug):
            calls.append(slug)
            return quote(107, 106, 107)

    live = LiveBooks(tape.path, Direct(), clock)
    record(tape, quote(106, 104, 102))
    assert asyncio.run(live.book(SLUG)).at == 106
    assert not calls

    assert asyncio.run(live.book(SLUG, requested_after=105)).at == 107
    assert calls == [SLUG]
    assert asyncio.run(live.book(SLUG, requested_after=105)).at == 107
    assert calls == [SLUG]


def test_stale_and_invalidated_cached_books_never_pass(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    now = [100.0]
    calls = []

    class Direct:
        async def book(self, slug):
            calls.append(slug)
            return quote(now[0], 99)

    live = LiveBooks(tape.path, Direct(), lambda: now[0])
    record(tape, quote(99, 93, 98))  # Received recently, exchange timestamp is stale.
    assert asyncio.run(live.book(SLUG)).at == 100
    tape.append(SLUG, 101, "rest", "gap", {})
    now[0] = 102
    with pytest.raises(FeedError, match="No fresh"):
        asyncio.run(live.book(SLUG))
    assert calls == [SLUG]
    now[0] = 106
    with pytest.raises(FeedError, match="freshness limits"):
        asyncio.run(live.book(SLUG))
    assert calls == [SLUG, SLUG]


@pytest.mark.parametrize("received_at,exchange_at,requested_at,accepted", [
    (100, 85, 99, True),
    (100, 84.99, 99, False),
    (96.99, 90, 96.99, False),
    (100, None, 99, False),
    (100, 90, 95.99, False),
])
def test_entry_exchange_age_boundary_keeps_other_freshness_checks(tmp_path, received_at, exchange_at,
                                                                 requested_at, accepted):
    tape = Tape(tmp_path / "tape.sqlite3")
    book = quote(received_at, exchange_at, requested_at)
    record(tape, book)
    books = LiveBooks(tape.path, None, lambda: 100)
    assert (books.entry_snapshot(SLUG, 96) is not None) is accepted
    assert not live_books.usable(book, 100)


def test_429_throttles_direct_calls_but_fresh_shared_book_can_recover(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    now = [100.0]
    calls = []

    class RateLimited:
        async def book(self, slug):
            calls.append(slug)
            response = httpx.Response(429, headers={"Retry-After": "10"},
                                      request=httpx.Request("GET", "https://example.invalid/book"))
            response.raise_for_status()

    live = LiveBooks(tape.path, RateLimited(), lambda: now[0])
    with pytest.raises(FeedError, match="HTTP 429"):
        asyncio.run(live.book(SLUG))
    now[0] = 104
    with pytest.raises(FeedError, match="HTTP 429"):
        asyncio.run(live.book(SLUG))
    record(tape, quote(103, 102, 102))
    assert asyncio.run(live.book(SLUG)).at == 103
    assert calls == [SLUG]


def test_entry_wait_recovers_from_rate_limit_using_new_recorded_book(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    observations, calls = [], []

    class RateLimited:
        async def book(self, slug):
            calls.append(slug)
            response = httpx.Response(429, headers={"Retry-After": "10"},
                                      request=httpx.Request("GET", "https://example.invalid/book"))
            response.raise_for_status()

    async def run():
        clock = asyncio.get_running_loop().time
        ready_at = clock()
        live = LiveBooks(tape.path, RateLimited(), clock)
        reported_429 = asyncio.Event()

        def observe(event):
            observations.append(event)
            if event["reason"] == "rate_limited":
                reported_429.set()

        task = asyncio.create_task(live.entry_book(SLUG, ready_at, ready_at + 1, observe))
        await asyncio.wait_for(reported_429.wait(), timeout=1)
        now = clock()
        record(tape, quote(now, now, now))
        assert (await task).at == now
        assert live.next_request > clock()

        with pytest.raises(BookUnavailable) as skipped:
            await live.entry_book(SLUG, now + .01, now + .08, observations.append)
        assert skipped.value.reason == "direct_cooldown"

    asyncio.run(run())
    assert calls == [SLUG]
    accepted = next(event for event in observations if event["reason"] == "usable")
    assert accepted["source"] == "recorded_rest"
    assert accepted["reason"] == "usable"
    assert accepted["receipt_age_seconds"] >= 0
    assert accepted["exchange_age_seconds"] >= 0
    assert accepted["request_after_ready_seconds"] >= 0
    assert [event["reason"] for event in observations if event.get("http_status") == 429] == ["rate_limited"]


def test_entry_does_not_select_book_until_account_checks_and_uses_newer_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")

    class Unused:
        async def book(self, slug):
            raise AssertionError("a usable local quote did not need a REST request")

    async def run():
        clock = asyncio.get_running_loop().time
        live = LiveBooks(tape.path, Unused(), clock)
        ready_at = clock()
        first = clock()
        record(tape, quote(first, first, first))
        checks_done, quote_seen = asyncio.Event(), asyncio.Event()

        def observe(event):
            if event["reason"] == "usable":
                quote_seen.set()

        task = asyncio.create_task(live.entry_book(SLUG, ready_at, ready_at + 1, observe,
                                                  can_select=checks_done.is_set))
        await asyncio.wait_for(quote_seen.wait(), timeout=1)
        assert not task.done()
        newer = clock()
        record(tape, quote(newer, newer, newer))
        checks_done.set()
        assert (await task).at == newer
        tape.append(SLUG, clock(), "rest", "closed", {})
        assert live.entry_snapshot(SLUG, ready_at) is None

    asyncio.run(run())


def test_entry_cancels_direct_read_when_recorded_quote_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")

    async def run():
        clock = asyncio.get_running_loop().time
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Stalled:
            async def book(self, slug):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        live = LiveBooks(tape.path, Stalled(), clock)
        ready_at = clock()
        task = asyncio.create_task(live.entry_book(SLUG, ready_at, ready_at + 1, lambda event: None))
        await asyncio.wait_for(started.wait(), timeout=1)
        now = clock()
        record(tape, quote(now, now, now))
        assert (await task).at == now
        assert cancelled.is_set()

    asyncio.run(run())


def test_entry_deadline_keeps_specific_freshness_details_and_bounds_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    observations, calls = [], []

    async def run():
        clock = asyncio.get_running_loop().time

        class Stale:
            async def book(self, slug):
                calls.append(slug)
                now = clock()
                return quote(now, now - 16, now)

        ready_at = clock()
        live = LiveBooks(tape.path, Stale(), clock)
        latest = clock()
        record(tape, quote(latest, latest, ready_at - .1))
        with pytest.raises(BookUnavailable, match="freshness limits") as skipped:
            await live.entry_book(SLUG, ready_at, ready_at + .1, observations.append)
        assert skipped.value.reason == "exchange_stale"

    asyncio.run(run())
    assert calls == [SLUG]
    rejected = [event for event in observations if event["reason"] in {
        "request_before_ready", "exchange_stale",
    }]
    assert [(event["source"], event["reason"]) for event in rejected] == [
        ("recorded_rest", "request_before_ready"), ("direct_rest", "exchange_stale"),
    ]
    assert rejected[0]["request_after_ready_seconds"] < 0
    assert rejected[1]["exchange_age_seconds"] >= 8


def test_direct_market_close_invalidates_older_snapshots_for_its_slug_only(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    now = [100.0]

    class Closed:
        async def book(self, slug):
            now[0] += .01
            raise FeedError("book MARKET_STATE_CLOSED")

    async def run():
        live = LiveBooks(tape.path, Closed(), lambda: now[0])
        old = quote(100, 100, 99)
        record(tape, old)
        tape.append("another-window", 100, "rest", "book", asdict(old))
        live.cached = (SLUG, old)
        assert live.entry_snapshot(SLUG, 99).at == old.at

        with pytest.raises(BookUnavailable, match="not open") as skipped:
            await live.entry_book(SLUG, 100, 100.05, lambda event: None)
        assert skipped.value.reason == "market_not_open"
        assert live.entry_snapshot(SLUG, 99) is None
        assert live.entry_snapshot("another-window", 99).at == old.at

        now[0] += .01
        record(tape, quote(now[0], now[0], now[0]))
        assert live.entry_snapshot(SLUG, 100).at == now[0]

    asyncio.run(run())


def test_rechecking_same_signal_cannot_dispatch_another_direct_read(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    now = [100.0]
    calls, observations = [], []

    async def run():
        started = asyncio.Event()

        class Stalled:
            async def book(self, slug):
                calls.append(slug)
                started.set()
                await asyncio.Future()

        live = LiveBooks(tape.path, Stalled(), lambda: now[0])
        task = asyncio.create_task(live.entry_book(SLUG, 100, 103.2, observations.append))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        now[0] = 103.1  # Direct spacing has elapsed, but the signal has not changed.
        with pytest.raises(BookUnavailable) as skipped:
            await live.entry_book(SLUG, 100, 103.2, observations.append)
        assert skipped.value.reason == "book_deadline"

    asyncio.run(run())
    assert calls == [SLUG]
    assert any(event["reason"] == "direct_attempt_already_used" for event in observations)


def test_entry_waits_for_more_stream_depth_without_using_conflicting_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    observations = []

    class Unused:
        async def book(self, slug):
            raise AssertionError("an eligible stream must suppress the REST fallback")

    async def run():
        ready_at = time.perf_counter()
        now = time.perf_counter()
        stream = replace(quote(now, now), yes_offers=((.40, .50),))
        later = time.perf_counter()
        record(tape, quote(later, later, later))
        thin_seen = asyncio.Event()

        def observe(event):
            observations.append(event)
            if event["reason"] == "insufficient_depth":
                thin_seen.set()

        def assess(candidate):
            depth = sum(qty for price, qty in candidate.yes_offers if price <= .40)
            return ("insufficient_depth" if depth < 4 else None), {"eligible_depth": depth}

        live = LiveBooks(tape.path, Unused(), time.perf_counter, stream_snapshot=lambda slug: (stream, False))
        task = asyncio.create_task(live.entry_book(SLUG, ready_at, ready_at + 1, observe, assess=assess))
        await asyncio.wait_for(thin_seen.wait(), timeout=1)
        assert not task.done()
        assert live.entry_snapshot(SLUG, ready_at, assess=assess) is None
        newer = time.perf_counter()
        stream = quote(newer, newer)
        assert await task == stream

    asyncio.run(run())
    assert [(event["source"], event["reason"], event["eligible_depth"]) for event in observations] == [
        ("stream", "insufficient_depth", .50), ("stream", "usable", 20),
    ]
    assert observations[-1]["requested_at"] is None
    assert observations[-1]["frame_after_ready_seconds"] >= 0


def test_fresh_stream_uses_entry_and_exit_specific_age_limits(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    record(tape, quote(100, 99, 99))
    old_exchange_time = quote(99.5, 90)
    current_exchange_time = quote(99.5, 97)
    stream = old_exchange_time
    live = LiveBooks(tape.path, None, lambda: 100, stream_snapshot=lambda slug: (stream, False))
    assert live.entry_snapshot(SLUG, 98) == old_exchange_time
    assert asyncio.run(live.book(SLUG)).exchange_at == 99
    stream = current_exchange_time
    assert asyncio.run(live.book(SLUG)) == current_exchange_time


def test_stale_stream_falls_back_once_and_must_arrive_after_signal_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    calls = []

    class Direct:
        async def book(self, slug):
            calls.append(slug)
            return quote(100, 100, 100)

    stale = quote(100, 84)
    live = LiveBooks(tape.path, Direct(), lambda: 100, stream_snapshot=lambda slug: (stale, False))
    assert live.entry_snapshot(SLUG, 99) is None
    assert asyncio.run(live.entry_book(SLUG, 99, 100.1, lambda event: None)).exchange_at == 100
    assert calls == [SLUG]

    # Even a populated request timestamp cannot replace a pre-ready stream frame receipt.
    stale = quote(99, 99, 100)
    live = LiveBooks(tape.path, None, lambda: 100, stream_snapshot=lambda slug: (stale, False))
    assert live.entry_snapshot(SLUG, 99.5) is None


def test_authoritative_stream_close_vetoes_rest_for_entry_and_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    record(tape, quote(100, 100, 100))

    class Unused:
        async def book(self, slug):
            raise AssertionError("a known closed stream must veto REST")

    live = LiveBooks(tape.path, Unused(), lambda: 100, stream_snapshot=lambda slug: (None, slug == SLUG))
    assert live.entry_snapshot(SLUG, 99) is None
    with pytest.raises(BookUnavailable) as exit_skip:
        asyncio.run(live.book(SLUG))
    assert exit_skip.value.reason == "market_not_open"
    with pytest.raises(BookUnavailable) as entry_skip:
        asyncio.run(live.entry_book(SLUG, 99, 100.05, lambda event: None))
    assert entry_skip.value.reason == "market_not_open"


def test_exit_rechecks_stream_market_state_after_direct_rest_response(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        state = (None, False)

        class Direct:
            async def book(self, slug):
                started.set()
                await release.wait()
                return quote(100, 100, 100)

        live = LiveBooks(tape.path, Direct(), lambda: 100, stream_snapshot=lambda slug: state)
        pending = asyncio.create_task(live.book(SLUG))
        await asyncio.wait_for(started.wait(), timeout=1)
        state = (None, True)
        release.set()
        with pytest.raises(BookUnavailable) as skipped:
            await pending
        assert skipped.value.reason == "market_not_open"

    asyncio.run(run())


def test_newer_confirmed_rest_close_blocks_earlier_stream_frame(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.append(SLUG, 99.5, "rest", "closed", {})
    # A late frame whose exchange time predates the closure cannot reopen the market.
    stream = quote(99.75, 99)
    live = LiveBooks(tape.path, None, lambda: 100, stream_snapshot=lambda slug: (stream, False))
    assert live.entry_snapshot(SLUG, 98) is None
    stream = quote(100, 99.75)
    assert live.entry_snapshot(SLUG, 98) == stream


def test_unqualified_quote_reports_specific_assessment_reason_at_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    live = LiveBooks(tape.path, None, lambda: 100, stream_snapshot=lambda slug: (quote(100, 100), False))
    with pytest.raises(BookUnavailable, match="Only 0.10 contracts fit") as skipped:
        asyncio.run(live.entry_book(SLUG, 99, 100.05, lambda event: None,
                                   assess=lambda book: ("budget_below_minimum", {"message": "Only 0.10 contracts fit"})))
    assert skipped.value.reason == "budget_below_minimum"


def test_rest_only_search_waits_for_new_depth_without_a_duplicate_request(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")

    class Unused:
        async def book(self, slug):
            raise AssertionError("a fresh quoted market should wait for locally captured depth")

    async def run():
        ready_at = time.perf_counter()
        now = time.perf_counter()
        record(tape, replace(quote(now, now, now), yes_offers=((.41, 10),)))
        rejected = asyncio.Event()

        def observe(event):
            if event["reason"] == "insufficient_depth":
                rejected.set()

        def assess(candidate):
            return ("insufficient_depth" if candidate.yes_offers[0][0] > .40 else None), {}

        live = LiveBooks(tape.path, Unused(), time.perf_counter)
        task = asyncio.create_task(live.entry_book(SLUG, ready_at, ready_at + 1, observe, assess=assess))
        await asyncio.wait_for(rejected.wait(), timeout=1)
        newer = time.perf_counter()
        record(tape, quote(newer, newer, newer))
        assert (await task).at == newer

    asyncio.run(run())


@pytest.mark.parametrize("stream", [None, quote(96.99, 99), quote(100, 84.99), quote(98, 98)])
def test_stream_only_never_reads_rest_when_ws_quote_is_unavailable(tmp_path, monkeypatch, stream):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    tape = Tape(tmp_path / "tape.sqlite3")
    # Represent both local and direct REST liquidity, each usable for entry and exit.
    rest = quote(100, 100, 100)
    record(tape, rest)

    class NoRest:
        async def book(self, slug):
            pytest.fail("WS-only quote checks must not start a REST book request")

    live = LiveBooks(tape.path, NoRest(), lambda: 100, stream_snapshot=lambda slug: (stream, False),
                     stream_only=True)
    live.cached = (SLUG, rest)

    def no_recorded(*args):
        pytest.fail("WS-only quote checks must not query the REST tape")

    monkeypatch.setattr(live, "_recorded", no_recorded)
    assert live.entry_snapshot(SLUG, 99) is None
    assert live.exit_snapshot(SLUG, 99) is None
    with pytest.raises(FeedError):
        asyncio.run(live.book(SLUG, requested_after=99))
    observations = []
    with pytest.raises(FeedError):
        asyncio.run(live.entry_book(SLUG, 99, 100.03, observations.append))
    assert observations and all(item["source"] == "stream" for item in observations)


def test_stream_only_entry_waits_for_fresh_frame_and_exit_retains_stricter_age(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)

    class NoRest:
        async def book(self, slug):
            pytest.fail("WS-only quote checks must not start a REST book request")

    async def run():
        clock = time.perf_counter
        ready = clock()
        before_ready = quote(ready - .05, ready - 1, requested_at=ready)
        stream = before_ready
        first_probe = asyncio.Event()

        def observe(event):
            if event["reason"] == "request_before_ready":
                first_probe.set()

        live = LiveBooks(tmp_path / "missing-tape", NoRest(), clock, stream_snapshot=lambda slug: (stream, False),
                         stream_only=True)
        wait = asyncio.create_task(live.entry_book(SLUG, ready, ready + 1, observe))
        await asyncio.wait_for(first_probe.wait(), timeout=1)
        assert not wait.done()

        now = clock()
        stream = quote(now, now - 7)
        assert await wait == stream
        assert live.entry_snapshot(SLUG, ready) == stream
        assert live.exit_snapshot(SLUG) is None
        with pytest.raises(FeedError):
            await live.book(SLUG)

        now = clock()
        stream = quote(now, now - 1)
        assert live.exit_snapshot(SLUG) == stream
        assert await live.book(SLUG) == stream

    asyncio.run(run())


def test_stream_only_retains_explicit_market_halt_on_entry_and_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(live_books, "TAPE_POLL_INTERVAL", .005)
    live = LiveBooks(tmp_path / "missing-tape", None, lambda: 100,
                     stream_snapshot=lambda slug: (quote(100, 100), True), stream_only=True)
    assert live.entry_snapshot(SLUG, 99) is None
    with pytest.raises(BookUnavailable) as entered:
        asyncio.run(live.entry_book(SLUG, 99, 100.03, lambda event: None))
    with pytest.raises(BookUnavailable) as exited:
        asyncio.run(live.book(SLUG))
    assert entered.value.reason == exited.value.reason == "market_not_open"
