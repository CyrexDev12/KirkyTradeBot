from contextlib import closing
from dataclasses import asdict
import json
import sqlite3
import sys

from backend.updown.strategy import Book, Window
from backend.wti import entry_replay
from backend.wti.tape import Tape


WINDOW = Window("wti", "wti-market", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point")


def signal(at, **overrides):
    return {"slug": WINDOW.slug, "signal_at": at, "ready_at": at + 2, "checks_completed_at": at + 3,
            "deadline": at + 8, "side": "DOWN", "limit": ".50", "minimum_quantity": "1",
            "depth_share": ".25", "max_exchange_age_seconds": 5, **overrides}


def journal_rows(path, timings):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE entry_timings(id INTEGER PRIMARY KEY, at REAL NOT NULL, payload TEXT NOT NULL)")
        db.executemany("INSERT INTO entry_timings(at,payload) VALUES (?,?)",
                       ((row["ready_at"] + 5, json.dumps(row)) for row in timings))


def add_book(tape, source, at, *, bid=.53, quantity=4, exchange_at=None, requested_at=None):
    book = Book(((bid, quantity),), ((.60, quantity),), at,
                at if exchange_at is None else exchange_at, requested_at)
    tape.append(WINDOW.slug, at, source, "book", asdict(book))


def test_entry_replay_classifies_distinct_signals_and_reports_earliest_eligibility(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    journal = tmp_path / "live.sqlite3"
    journal_rows(journal, [signal(at) for at in (1000, 1100, 1200, 1300)]
                 + [signal(1000, checks_completed_at=1007)])

    add_book(tape, "rest", 1002.5, requested_at=1002.25)
    add_book(tape, "stream", 1003.5, bid=.49)
    add_book(tape, "stream", 1004, quantity=2)
    add_book(tape, "stream", 1006, quantity=8)
    add_book(tape, "rest", 1104)
    add_book(tape, "stream", 1104, exchange_at=1098)
    add_book(tape, "rest", 1204, requested_at=1201.9)
    add_book(tape, "stream", 1205)
    add_book(tape, "rest", 1303.5, bid=.49)
    add_book(tape, "stream", 1303.5, quantity=3)

    data = entry_replay.report(journal, tape.path, 900, 1400)
    assert data["attempts"] == 5
    assert data["signals"] == data["evaluated_signals"] == 4
    assert data["coverage"] == {"both": 1, "rest_only": 1, "stream_only": 1, "neither": 1}
    assert data["sources"]["rest"]["outcomes"] == {"eligible": 2, "stale": 1, "price_above_limit": 1}
    assert data["sources"]["stream"]["outcomes"] == {"eligible": 2, "stale": 1, "insufficient_depth": 1}
    assert data["signal_details"][0]["rest"] == {"outcome": "eligible", "earliest_eligible_at": 1003,
                                               "seconds_left": 5, "seconds_since_checks": 0}
    assert data["signal_details"][0]["stream"]["earliest_eligible_at"] == 1006
    assert data["sources"]["stream"]["seconds_left_at_first_eligible"] == {"samples": 2, "median": 2.5, "p95": 3}


def test_each_attempt_uses_its_own_exchange_age_limit(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    journal = tmp_path / "live.sqlite3"
    journal_rows(journal, [signal(1000), signal(1000, checks_completed_at=1004, max_exchange_age_seconds=15)])
    add_book(tape, "stream", 1002.5, exchange_at=993)
    add_book(tape, "rest", 1002.5, exchange_at=993)

    data = entry_replay.report(journal, tape.path, 900, 1100)
    assert data["coverage"]["both"] == 1
    assert data["signal_details"][0]["rest"]["earliest_eligible_at"] == 1004
    assert data["signal_details"][0]["stream"]["earliest_eligible_at"] == 1004


def test_account_checks_cannot_reuse_receipt_older_than_three_seconds(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    journal = tmp_path / "live.sqlite3"
    journal_rows(journal, [signal(1000, checks_completed_at=1006)])
    add_book(tape, "rest", 1002.75)
    add_book(tape, "stream", 1003)

    data = entry_replay.report(journal, tape.path, 900, 1100)
    assert data["signal_details"][0]["rest"]["outcome"] == "stale"
    assert data["signal_details"][0]["stream"]["earliest_eligible_at"] == 1006


def test_replay_rejects_outdated_depth_closed_books_and_exact_cutoff(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    journal = tmp_path / "live.sqlite3"
    journal_rows(journal, [signal(1787, checks_completed_at=1791)])
    add_book(tape, "stream", 1790, exchange_at=1789.5)
    tape.append(WINDOW.slug, 1791, "stream", "trade", {"exchange_at": 1790.5})
    add_book(tape, "stream", 1791.5, exchange_at=1790)
    add_book(tape, "rest", 1790.5)
    tape.append(WINDOW.slug, 1791, "rest", "closed", {})
    for source in ("stream", "rest"):
        add_book(tape, source, 1793)

    data = entry_replay.report(journal, tape.path, 900, 1800)
    assert data["coverage"]["neither"] == 1
    assert data["sources"]["rest"]["outcomes"] == {"market_not_open": 1}
    assert data["sources"]["stream"]["outcomes"] == {"no_book": 1}


def test_replay_inherits_stream_repeat_and_reopen_guards(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    journal = tmp_path / "live.sqlite3"
    journal_rows(journal, [signal(1000), signal(1100), signal(1200)])
    add_book(tape, "stream", 1001.75, exchange_at=1001.5)
    add_book(tape, "stream", 1003.5, exchange_at=1001.5)
    add_book(tape, "stream", 1005)

    tape.append(WINDOW.slug, 1102.5, "stream", "closed", {"exchange_at": 1102.25})
    add_book(tape, "stream", 1103.5, exchange_at=1102.25)
    add_book(tape, "stream", 1105, exchange_at=1102.5)

    tape.append(WINDOW.slug, 1202.5, "stream", "closed", {})
    add_book(tape, "stream", 1203.5, exchange_at=1202)

    data = entry_replay.report(journal, tape.path, 900, 1300)
    assert data["signal_details"][0]["stream"]["earliest_eligible_at"] == 1005
    assert data["signal_details"][1]["stream"]["earliest_eligible_at"] == 1105
    assert data["signal_details"][2]["stream"]["outcome"] == "market_not_open"


def test_confirmed_account_and_risk_blocks_are_separate_from_quote_coverage(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    journal = tmp_path / "live.sqlite3"
    journal_rows(journal, [signal(1000, reason="account_has_exposure"),
                           signal(1010, reason="budget_below_minimum"),
                           signal(1020, max_order_quantity=".25"),
                           signal(1030, reason="invalid_limit"),
                           signal(1040, max_order_quantity="1", reason="price_above_limit")])
    for at in (1000, 1010, 1020, 1030, 1040):
        add_book(tape, "stream", at + 4)

    data = entry_replay.report(journal, tape.path, 900, 1100)
    assert data["signals"] == 5 and data["evaluated_signals"] == 1
    assert data["unassessable_signals"] == {"account_has_exposure": 1, "budget_below_minimum": 2,
                                          "invalid_limit": 1}
    assert data["coverage"] == {"both": 0, "rest_only": 0, "stream_only": 1, "neither": 0}


def test_unassessable_signals_do_not_count_as_neither(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    journal = tmp_path / "live.sqlite3"
    journal_rows(journal, [signal(1000, checks_completed_at=None), signal(1010, minimum_quantity=None),
                           signal(1020, checks_completed_at=1028), signal(1030, slug="not-recorded"),
                           signal(1040, signal_at=None), signal(1050)])

    data = entry_replay.report(journal, tape.path, 900, 1057.5)
    assert data["evaluated_signals"] == 0
    assert data["attempts_missing_identity"] == 1
    assert data["signals"] == 5
    assert data["unassessable_signals"] == {"no_account_checks_completed": 1, "missing_or_invalid_contract_terms": 1,
                                          "checks_completed_at_or_after_cutoff": 1, "market_not_recorded": 1,
                                          "cutoff_not_reached": 1}
    assert data["coverage"]["neither"] == 0


def test_cli_returns_read_only_empty_report_when_databases_are_missing(tmp_path, monkeypatch, capsys):
    journal, tape = tmp_path / "unconfigured-journal", tmp_path / "unconfigured-tape"
    monkeypatch.setattr(sys, "argv", ["entry_replay", "--hours", "24", "--journal", str(journal),
                                     "--tape", str(tape), "--json"])
    entry_replay.main()
    result = json.loads(capsys.readouterr().out)
    assert result["attempts"] == 0
    assert result["evaluated_signals"] == 0
    assert not journal.exists() and not tape.exists()
