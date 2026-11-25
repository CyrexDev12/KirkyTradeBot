import asyncio
from base64 import b64encode
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sqlite3
import sys

from backend.updown.strategy import Book, Window
from backend.wti import replay_capture, shadow
from backend.wti.replay_capture import (
    MarketStream, SHADOW_HEALTH_FILE, StreamBookSource, current_window, run_shadow_stream, stream_row,
)
from backend.wti.shadow import report
from backend.wti.tape import Tape, tape_path


WINDOW = Window("wti", "current-oil", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point")


def book_row(at, exchange_at, requested_at=None, sequence=None):
    return asdict(Book(((.45, 10),), ((.47, 10),), at, exchange_at, requested_at, sequence))


def test_live_stream_handle_preserves_receipt_and_invalidates_order_regressions_and_trades():
    now = [1100]
    source = StreamBookSource(lambda: now[0])
    generation = source.begin(WINDOW)

    def book(at, exchange_at, sequence):
        now[0] = at
        source.observe(generation, (WINDOW.slug, at, "stream", "book", book_row(at, exchange_at, sequence=sequence)))

    book(1100, 1090, 10)
    assert source.snapshot(WINDOW.slug) == (None, False)
    source.set_connected(generation, True)
    book(1100, 1090, 10)
    quote, non_open = source.snapshot(WINDOW.slug)
    assert not non_open and (quote.at, quote.exchange_at, quote.requested_at) == (1100, 1090, None)
    assert source.snapshot("another-slug") == (None, False)
    book(1101, 1090, 10)
    assert source.snapshot(WINDOW.slug)[0].at == 1100
    now[0] = 1103.001
    assert source.snapshot(WINDOW.slug) == (None, False)
    book(1104, 1103, 11)
    book(1104.1, 1103.5, 10)
    assert source.snapshot(WINDOW.slug) == (None, False)
    book(1104.2, 1103.5, 12)
    assert source.snapshot(WINDOW.slug)[0].sequence == 12
    source.observe(generation, (WINDOW.slug, 1104.2, "stream", "trade", {"exchange_at": 1104}))
    assert source.snapshot(WINDOW.slug) == (None, False)
    book(1104.3, 1103.9, 13)
    assert source.snapshot(WINDOW.slug) == (None, False)
    book(1104.4, 1104.2, 14)
    assert source.snapshot(WINDOW.slug)[0].exchange_at == 1104.2
    source.observe(generation, (WINDOW.slug, 1104.4, "stream", "gap", {}))
    assert source.snapshot(WINDOW.slug) == (None, False)
    book(1104.5, 1104.2, 14)
    assert source.snapshot(WINDOW.slug) == (None, False)


def test_live_stream_halt_survives_reconnect_until_new_open_book_and_expires_at_rollover():
    now = [1100]
    source = StreamBookSource(lambda: now[0])
    generation = source.begin(WINDOW)
    source.set_connected(generation, True)
    source.observe(generation, (WINDOW.slug, 1100, "stream", "closed", {"exchange_at": 1099}))
    assert source.snapshot(WINDOW.slug) == (None, True)
    source.set_connected(generation, False)
    assert source.snapshot(WINDOW.slug) == (None, True)

    reconnected = source.begin(WINDOW)
    source.set_connected(reconnected, True)
    now[0] = 1100.5
    for token, venue_at in ((generation, 1100.25), (reconnected, 1099)):
        frame = book_row(now[0], venue_at)
        source.observe(token, (WINDOW.slug, now[0], "stream", "book", frame))
        assert source.snapshot(WINDOW.slug) == (None, True)
    source.observe(reconnected, (WINDOW.slug, now[0], "stream", "book", book_row(now[0], 1100.25)))
    assert source.snapshot(WINDOW.slug)[0].at == now[0]
    source.observe(reconnected, (WINDOW.slug, now[0], "stream", "closed", {}))
    now[0] = 1101
    source.observe(reconnected, (WINDOW.slug, now[0], "stream", "book", book_row(now[0], 1100.4)))
    assert source.snapshot(WINDOW.slug) == (None, True)

    now[0] = WINDOW.end
    assert source.snapshot(WINDOW.slug) == (None, False)
    next_window = Window("wti", "next-oil", 1800, 2700, 80, "MARKET_STATUS_OPEN", .095)
    next_generation = source.begin(next_window)
    source.set_connected(next_generation, True)
    source.observe(next_generation, (WINDOW.slug, now[0], "stream", "closed", {}))
    source.observe(next_generation, (next_window.slug, now[0], "rest", "closed", {}))
    assert source.snapshot(next_window.slug) == (None, False)


def test_partial_and_lite_stream_frames_cannot_become_executable_books():
    open_data = {"marketSlug": WINDOW.slug, "state": "MARKET_STATE_OPEN",
                 "bids": [{"px": {"value": ".45"}, "qty": "5"}]}
    assert stream_row({"marketData": open_data}, WINDOW.slug, 1100)[3] == "gap"
    assert stream_row({"marketDataLite": open_data}, WINDOW.slug, 1100) is None
    assert stream_row({"marketData": {**open_data, "state": "MARKET_STATE_UNKNOWN"}}, WINDOW.slug, 1100)[3] == "gap"
    assert stream_row({"marketData": {**open_data, "state": "MARKET_STATE_HALTED"}}, WINDOW.slug, 1100)[3] == "closed"


def test_shadow_socket_publishes_in_memory_and_clears_before_flushing_tape(monkeypatch, tmp_path):
    monkeypatch.delenv("WTI_REPLAY_KEY_ID", raising=False)
    monkeypatch.delenv("WTI_REPLAY_SECRET_KEY", raising=False)
    tape = Tape(tape_path(tmp_path))
    tape.record_window(WINDOW)

    async def run():
        source = StreamBookSource(lambda: 1100)
        stop, received = asyncio.Event(), asyncio.Event()
        subscriptions = []
        connect_calls = []
        payload = {"marketData": {"marketSlug": WINDOW.slug, "state": "MARKET_STATE_OPEN",
                                  "transactTime": datetime.fromtimestamp(1099, timezone.utc).isoformat(),
                                  "bids": [{"px": {"value": ".45"}, "qty": "5"}],
                                  "offers": [{"px": {"value": ".47"}, "qty": "3"}]}}
        publish = source.observe

        def observe(generation, row):
            publish(generation, row)
            received.set()

        monkeypatch.setattr(source, "observe", observe)

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def send(self, message):
                subscriptions.append(json.loads(message)["subscribe"])

            async def recv(self):
                if not received.is_set():
                    return json.dumps(payload)
                await asyncio.Future()

        def connect(url, **_):
            connect_calls.append(url)
            return Socket()

        monkeypatch.setattr(replay_capture, "connect", connect)
        observer = asyncio.create_task(run_shadow_stream(
            tmp_path, stop, clock=lambda: 1100, credentials=("test-key", b64encode(b"k" * 32).decode()),
            quote_source=source,
        ))
        try:
            await asyncio.wait_for(received.wait(), timeout=2)
            live, veto = await asyncio.to_thread(source.snapshot, WINDOW.slug)
            assert not veto and (live.at, live.exchange_at) == (1100, 1099)
            with sqlite3.connect(tape.path) as db:
                assert not db.execute("SELECT 1 FROM tape WHERE source='stream' AND kind='book'").fetchone()
        finally:
            stop.set()
            await asyncio.wait_for(observer, timeout=2)
        assert source.snapshot(WINDOW.slug) == (None, False)
        assert len(connect_calls) == 1
        assert {item["subscriptionType"] for item in subscriptions} == {
            "SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE",
        }
        with sqlite3.connect(tape.path) as db:
            assert db.execute("SELECT at FROM tape WHERE source='stream' AND kind='book'").fetchone() == (1100,)

    asyncio.run(run())


def test_blocked_tape_writer_cannot_delay_halt_and_overflow_marks_earliest_lost_row(monkeypatch, tmp_path):
    monkeypatch.setattr(replay_capture, "STREAM_BATCH_ROWS", 1)
    monkeypatch.setattr(replay_capture, "STREAM_BUFFER_ROWS", 3)
    tape = Tape(tape_path(tmp_path))
    tape.record_window(WINDOW)

    async def run():
        now = [1100]
        clock = lambda: now[0]
        source = StreamBookSource(clock)
        generation = source.begin(WINDOW)
        source.set_connected(generation, True)
        stream = MarketStream(tape, clock, quote_source=source)
        slow, release, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        inbound = asyncio.Queue()
        write, publish = stream._write, source.observe

        async def first_write_waits(method, *args):
            if not slow.is_set():
                slow.set()
                await release.wait()
            await write(method, *args)

        def observe(token, row):
            publish(token, row)
            observed.set()

        class Socket:
            async def recv(self):
                return await inbound.get()

        monkeypatch.setattr(stream, "_write", first_write_waits)
        monkeypatch.setattr(source, "observe", observe)
        task = asyncio.create_task(stream._stream_messages(Socket(), WINDOW, generation))

        async def send(at, state):
            now[0] = at
            frame = {"marketData": {"marketSlug": WINDOW.slug, "state": state,
                                    "transactTime": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                                    "bids": [{"px": {"value": ".45"}, "qty": "5"}],
                                    "offers": [{"px": {"value": ".47"}, "qty": "3"}]}}
            observed.clear()
            inbound.put_nowait(json.dumps(frame))
            await asyncio.wait_for(observed.wait(), timeout=2)

        try:
            await send(1100, "MARKET_STATE_OPEN")
            await asyncio.wait_for(slow.wait(), timeout=2)
            for at in (1101, 1102, 1103):
                await send(at, "MARKET_STATE_OPEN")
            await send(1104, "MARKET_STATE_HALTED")
            assert source.snapshot(WINDOW.slug) == (None, True)
            with sqlite3.connect(tape.path) as db:
                assert not db.execute("SELECT 1 FROM tape").fetchone()
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)

        with sqlite3.connect(tape.path) as db:
            rows = db.execute("SELECT at,kind,payload FROM tape ORDER BY at,id").fetchall()
        assert [(at, kind) for at, kind, _ in rows] == [(1100, "book"), (1101, "gap"), (1104, "closed")]
        assert json.loads(rows[1][2]) == {"reason": "tape_buffer_overflow"}
        assert source.snapshot(WINDOW.slug) == (None, True)

    asyncio.run(run())


def test_report_compares_exact_first_fresh_quotes_for_complete_oil_d_intents(tmp_path):
    tape = Tape(tape_path(tmp_path))
    tape.record_window(WINDOW)
    for signal_at, deadline in ((1100, 1700), (1200, 1700), (1300, 1310), (1400, 1700)):
        tape.append(WINDOW.slug, signal_at, "intent", "order", {
            "model": "D", "action": "buy", "side": "DOWN", "ready_at": signal_at + 2, "deadline": deadline,
        })
    tape.append(WINDOW.slug, 1100, "intent", "order", {
        "model": "A", "action": "buy", "side": "UP", "ready_at": 1102, "deadline": 1700,
    })
    tape.append("unexpected-slug", 1102.1, "stream", "book", book_row(1102.1, 1102))
    tape.append(WINDOW.slug, 1101.5, "stream", "book", book_row(1101.5, 1101))
    tape.append(WINDOW.slug, 1102.1, "stream", "gap", {})
    tape.append(WINDOW.slug, 1102.15, "stream", "trade", {"exchange_at": 1102})
    tape.append(WINDOW.slug, 1102.2, "stream", "book", book_row(1102.2, 1101.8))
    tape.append(WINDOW.slug, 1103.25, "stream", "book", book_row(1103.25, 1102.5))
    tape.append(WINDOW.slug, 1102.5, "rest", "book", book_row(1102.5, 1102, 1101.999))
    tape.append(WINDOW.slug, 1105, "rest", "book", book_row(1105, 1104, 1103))
    tape.append(WINDOW.slug, 1202.5, "rest", "book", book_row(1202.5, 1196, 1202))
    tape.append(WINDOW.slug, 1207, "rest", "book", book_row(1207, 1202, 1206))
    tape.append(WINDOW.slug, 1208.01, "stream", "book", book_row(1208.01, 1206))
    tape.append(WINDOW.slug, 1308, "stream", "book", book_row(1308, 1307))

    result = report(tape.path, 900, 1404)
    assert result["eligible_d_buy_intents"] == 3
    assert result["both_sources_fresh"] == 1
    assert result["first_fresh"] == {"rest": 1, "stream": 1, "tie": 0, "neither": 1}
    assert result["sources"]["rest"]["fresh_intents"] == 2
    assert result["sources"]["rest"]["p95_ready_to_quote_seconds"] == 5
    assert result["sources"]["rest"]["rejected_books"] == 1
    assert result["sources"]["stream"]["fresh_intents"] == 1
    assert result["sources"]["stream"]["median_ready_to_quote_seconds"] == 1.25
    assert result["sources"]["stream"]["median_venue_to_receipt_seconds"] == .75
    assert result["sources"]["stream"]["gap_events"] == 1
    assert result["sources"]["stream"]["rejected_books"] == 1


def test_stream_health_expires_on_original_timestamps_and_invalidates_trades(monkeypatch, tmp_path):
    monkeypatch.delenv("WTI_REPLAY_KEY_ID", raising=False)
    monkeypatch.delenv("WTI_REPLAY_SECRET_KEY", raising=False)
    now = 1100
    stream = MarketStream(Tape(tape_path(tmp_path)), lambda: now, ("shadow-key", b64encode(b"k" * 32).decode()))
    stream.window, stream.state = WINDOW, "connected"
    stream.quotes.update(1100, "book", book_row(1100, 1099.5, sequence=10))
    assert stream.health()["state"] == "fresh"
    assert stream.health()["last_book_exchange_at"] == 1099.5
    now = 1103.001
    assert stream.health()["state"] == "no_fresh_book"
    stream.quotes.update(1103.5, "book", book_row(1103.5, 1103, sequence=11))
    now = 1104
    assert stream.health()["state"] == "fresh"
    stream.quotes.update(now, "trade", {"exchange_at": 1103.7})
    assert stream.health()["state"] == "no_fresh_book"
    stream.quotes.update(now, "book", book_row(now, 1103.8, sequence=10))
    assert stream.health()["state"] == "no_fresh_book"
    stream.quotes.update(now, "book", book_row(now, 1103.8, sequence=12))
    assert stream.health()["state"] == "fresh"
    stream.quotes.update(now, "gap", {})
    assert stream.health()["state"] == "no_fresh_book"
    now = WINDOW.end
    assert stream.health()["state"] == "waiting_for_market"
    assert stream.health()["slug"] is None


def test_standalone_shadow_recovers_tape_and_tracks_only_current_wti_window(monkeypatch, tmp_path):
    tape = Tape(tape_path(tmp_path))
    tape.record_window(WINDOW)
    tape.record_window(Window("wti", "old-oil", 0, 900, 80, "MARKET_STATUS_OPEN", .095))
    tape.record_window(Window("wti", "future-oil", 1800, 2700, 80, "MARKET_STATUS_OPEN", .095))
    tape.record_window(Window("gold", "gold-other", 901, 1801, 80, "MARKET_STATUS_OPEN", .095))
    assert current_window(tape.path, 1800).slug == "future-oil"
    assert current_window(tape.path, 2700) is None
    attempts = []

    def open_tape(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("temporary tape startup failure")
        return Tape(path)

    monkeypatch.setattr(replay_capture, "Tape", open_tape)
    monkeypatch.setattr(replay_capture, "SHADOW_POLL_SECONDS", .001)

    async def observe():
        stop, started, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        received = []

        class FakeStream:
            configured = True

            def __init__(self, _tape, _clock, credentials):
                assert credentials == ("test-key", "test-secret")

            async def run(self, window):
                received.append(window)
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

            def health(self):
                return {"state": "test", "mode": "shadow", "source": "stream"}

        observer = asyncio.create_task(run_shadow_stream(
            tmp_path, stop, clock=lambda: 1200, credentials=("test-key", "test-secret"), stream_factory=FakeStream,
        ))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
        finally:
            stop.set()
            await asyncio.wait_for(observer, timeout=2)
        assert received == [WINDOW]
        assert cancelled.is_set()
        assert attempts == [tape.path, tape.path]
        assert json.loads(tape.path.with_name(SHADOW_HEALTH_FILE).read_text())["mode"] == "shadow"

    asyncio.run(observe())


def test_empty_shadow_report_has_no_fabricated_coverage_or_quotes(tmp_path):
    metrics = report(tape_path(tmp_path), 0, 100)
    assert metrics["eligible_d_buy_intents"] == 0 and metrics["markets"] == 0
    for source in metrics["sources"].values():
        assert source["coverage"] is None and source["median_ready_to_quote_seconds"] is None


def test_shadow_cli_uses_compose_data_directory(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WTI_DATA_DIR", raising=False)
    tape = Tape(tape_path(tmp_path / "wti"))
    tape.record_window(WINDOW)
    monkeypatch.setattr(shadow.time, "time", lambda: 1400)
    monkeypatch.setattr(sys, "argv", ["shadow", "--hours", "1", "--json"])
    shadow.main()
    result = json.loads(capsys.readouterr().out)
    assert result["markets"] == 1
