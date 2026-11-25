from dataclasses import asdict
import json
import sqlite3
import sys

import pytest

from backend.updown.strategy import Book, Window
from backend.wti import live_timing
from backend.wti.live_store import live_path
from backend.wti.tape import Tape, tape_path


WINDOW = Window("wti", "wti-market", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point")


def journal_rows(path, rows):
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE entry_timings(id INTEGER PRIMARY KEY, at REAL NOT NULL, payload TEXT NOT NULL)")
        db.executemany("INSERT INTO entry_timings(at,payload) VALUES (?,?)",
                       ((at, json.dumps(payload)) for at, payload in rows))


def add_book(tape, source, at, venue_at, qty, requested_at=None):
    book = Book(((.53, qty),), ((.59, qty),), at, venue_at, requested_at)
    tape.append(WINDOW.slug, at, source, "book", asdict(book))


def test_live_timing_aggregates_attempts_and_compares_books_at_logged_instants(tmp_path):
    journal = live_path(tmp_path)
    tape = Tape(tape_path(tmp_path))
    tape.record_window(WINDOW)
    entry = {
        "slug": WINDOW.slug, "side": "DOWN", "signal_at": 1000, "ready_at": 1002, "deadline": 1008,
        "limit": ".50", "minimum_quantity": "1", "depth_share": ".25",
        "attempt_started_at": 1002.25, "checks_completed_at": 1003, "book_search_started_at": 1003,
        "quote_selected_at": 1004, "decision_at": 1004.5, "submit_started_at": 1005.1,
        "attempt_finished_at": 1005.5, "outcome": "submitted", "order_state": "ORDER_STATE_NEW",
        "acknowledged": True, "acknowledged_at": 1005.45, "filled": "0", "quotes": [
            {"source": "rest_tape", "stage": "capture", "result": "usable", "checked_at": 1004,
             "received_at": 1003.1, "exchange_at": 1003, "requested_at": 1002.9},
            {"source": "rest_direct", "stage": "direct", "result": "rejected", "http_status": 429,
             "reason": "rate_limited", "observed_at": 1004, "wait_seconds": .125},
            {"source": "rest_direct", "stage": "direct", "result": "rejected", "http_status": 429,
             "reason": "direct_cooldown", "observed_at": 1004.25},
        ],
    }
    skipped = {
        "slug": WINDOW.slug, "signal_at": 1000, "ready_at": 1002, "deadline": 1008,
        "attempt_started_at": 1005.5, "checks_completed_at": 1008.001,
        "book_search_started_at": 1006, "book_search_finished_at": 1008,
        "attempt_finished_at": 1008.001, "outcome": "skipped", "reason": "expired",
    }
    journal_rows(journal, [(899, skipped), (1005.5, entry), (1008.001, skipped), (2000, entry)])

    add_book(tape, "stream", 1002.4, 1002.2, 20)
    add_book(tape, "rest", 1002.5, 1002.2, 20, requested_at=1001)
    add_book(tape, "rest", 1003.1, 1003, 2, requested_at=1002.9)
    tape.append(WINDOW.slug, 1003.2, "stream", "trade", {"exchange_at": 1003.1})
    add_book(tape, "stream", 1003.3, 1002.5, 20)
    add_book(tape, "stream", 1004.25, 1004, 20)
    tape.append(WINDOW.slug, 1005, "stream", "gap", {})
    add_book(tape, "stream", 1005.2, 1005.2, 20)

    result = live_timing.report(journal, tape.path, 900, 1800)
    assert result["attempts"] == 2 and result["signals"] == 1
    assert result["outcomes"] == {"submitted": 1, "skipped": 1}
    assert result["reasons"] == {"expired": 1}
    assert result["acknowledged"] == 1 and result["order_states"] == {"ORDER_STATE_NEW": 1}
    assert result["initial_responses"] == {"with_fills": 0, "terminal_no_fill": 0, "pending": 1, "unconfirmed": 0}
    assert result["phases_seconds"]["book_search"] == {"samples": 2, "median": 1.5, "p95": 2}
    assert result["phases_seconds"]["submission_to_acknowledgment"]["median"] == .35
    assert result["phases_seconds"]["submission_to_finish"]["median"] == .4
    assert result["quotes"]["rest_tape"]["seconds"]["receipt_age"]["median"] == .9
    assert result["quotes"]["rest_direct"]["http_status"] == {"429": 1}
    assert result["quotes"]["rest_direct"]["reasons"]["direct_cooldown"] == 1
    assert result["quotes"]["rest_direct"]["seconds"]["wait"]["median"] == .125

    comparison = result["recorded_coverage"]
    assert comparison["markets"] == 1
    probes = comparison["probes"]
    assert probes["checks_completed"]["fresh"]["stream_only"] == 1
    assert probes["checks_completed"]["fresh_with_minimum_depth"]["stream_only"] == 1
    assert probes["quote_selected"]["fresh"]["rest_only"] == 1
    assert probes["quote_selected"]["fresh_with_minimum_depth"]["neither"] == 1
    assert probes["decision"]["fresh"]["both"] == 1
    assert probes["decision"]["fresh_with_minimum_depth"]["stream_only"] == 1
    assert probes["submission"]["fresh"]["rest_only"] == 1


def test_initial_responses_distinguish_zero_fill_pending_and_unknown():
    responses = [
        {"filled": ".5", "acknowledged": True, "order_state": "ORDER_STATE_CANCELED"},
        {"filled": "0", "acknowledged": True, "order_state": "ORDER_STATE_CANCELED"},
        {"filled": "0", "acknowledged": True, "order_state": "ORDER_STATE_PENDING_NEW"},
        {"order_state": "UNKNOWN"},
    ]
    assert live_timing.initial_responses([{**row, "submit_started_at": 100} for row in responses]) == {
        "with_fills": 1, "terminal_no_fill": 1, "pending": 1, "unconfirmed": 1,
    }


def test_coverage_uses_the_age_limit_at_each_attempt(tmp_path):
    journal = live_path(tmp_path)
    tape = Tape(tape_path(tmp_path))
    tape.record_window(WINDOW)
    entry = {"slug": WINDOW.slug, "side": "DOWN", "signal_at": 1000, "ready_at": 1002, "deadline": 1008,
             "limit": ".50", "minimum_quantity": "1", "depth_share": ".25", "checks_completed_at": 1003}
    journal_rows(journal, [(1003, entry), (1004, {**entry, "max_exchange_age_seconds": 15})])
    add_book(tape, "rest", 1002.5, 991, 20, requested_at=1002)
    add_book(tape, "stream", 1002.8, 991, 20)

    result = live_timing.report(journal, tape.path, 900, 1800)
    assert result["exchange_age_limits_seconds"] == {"5": 1, "15": 1}
    counts = result["recorded_coverage"]["probes"]["checks_completed"]
    assert counts["fresh_with_minimum_depth"] == {"rest_only": 0, "stream_only": 0, "both": 1, "neither": 1}


def test_missing_or_old_journal_returns_no_data_without_creating_a_database(tmp_path):
    journal, tape = live_path(tmp_path), tape_path(tmp_path)
    result = live_timing.report(journal, tape, 0, 100)
    assert result["attempts"] == 0
    assert not journal.exists() and not tape.exists()
    journal.parent.mkdir()
    with sqlite3.connect(journal) as db:
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, at REAL, payload TEXT)")
    assert live_timing.report(journal, tape, 0, 100)["attempts"] == 0
    with live_timing.readonly(journal) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("INSERT INTO events (at,payload) VALUES (100,'{}')")


def test_cli_reads_the_compose_data_directory_and_respects_lookback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WTI_DATA_DIR", raising=False)
    monkeypatch.setattr(live_timing.time, "time", lambda: 8000)
    journal_rows(live_path(tmp_path / "wti"), [(4399, {"outcome": "old"}), (4500, {"outcome": "skipped"})])
    monkeypatch.setattr(sys, "argv", ["live_timing", "--hours", "1", "--json", "--attempts"])
    live_timing.main()
    result = json.loads(capsys.readouterr().out)
    assert result["outcomes"] == {"skipped": 1}
    assert result["recorded_coverage"]["markets"] == 0
    assert result["attempt_details"] == [{"outcome": "skipped"}]

    monkeypatch.setattr(sys, "argv", ["live_timing", "--hours", "1"])
    live_timing.main()
    output = capsys.readouterr().out
    assert "1 attempt |" in output and "No recorded market quotes" in output
