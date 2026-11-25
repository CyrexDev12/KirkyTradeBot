from base64 import b64decode
from dataclasses import asdict
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.updown.feed import Settlement
from backend.updown.strategy import Book, Window
from backend.wti.models import MODEL_INFO
from backend.wti.replay import BookCursor, compare, report_text
from backend.wti.replay_capture import WS_PATH, stream_headers, stream_row
from backend.wti.tape import Tape


WINDOW = Window("wti", "wti-window", 1000, 1900, 80, "MARKET_STATUS_OPEN", .1, rule="point")


def quote(tape, when, bid, ask, size=20, source="rest", exchange_at=None):
    book = Book(((bid, size),), ((ask, size),), when, exchange_at=exchange_at)
    tape.append(WINDOW.slug, when, source, "book", asdict(book))


def intent(tape, model, at, action="buy"):
    row = {"model": model, "action": action, "side": "UP", "quantity": 10, "theta": .1,
           "ready_at": at + 2, "deadline": WINDOW.end - 90 if action == "buy" else WINDOW.end}
    if action == "buy":
        row.update(limit=.46, fair_up=.80, min_edge=.02, budget=5.0)
    else:
        row.update(limit=.55, exit_kind="target")
    tape.append(WINDOW.slug, at, "intent", "order", row)


def test_replays_all_five_models_at_four_delay_and_depth_settings(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    for model in MODEL_INFO:
        intent(tape, model, 1100)
        intent(tape, model, 1200, "sell")
    quote(tape, 1101, .39, .40)
    quote(tape, 1104, .44, .45, size=8)
    quote(tape, 1201, .60, .61)
    quote(tape, 1204, .58, .59, size=8)
    tape.record_settlement(Settlement(WINDOW.slug, 1, 80, 80.1), 1905)

    report = compare(tape.path, 999, 2000)
    assert report["source"] == "rest" and list(report["models"]) == list("ABCDE")
    for model in report["models"].values():
        a, b, c, d = model["scenarios"].values()
        assert [v["scored_windows"] for v in (a, b, c, d)] == [1] * 4
        assert [v["entry"]["filled_contracts"] for v in (a, b, c, d)] == [10, 4, 5, 2]
        assert [v["exit"]["filled_contracts"] for v in (a, b, c, d)] == [10, 4, 5, 2]
        assert [v["entry"]["average_fill_price"] for v in (a, b, c, d)] == [.40, .45, .40, .45]
        assert a["net_pnl"] == pytest.approx(10 * (.60 - .40 - .024 - .024))
        assert b["net_pnl"] == pytest.approx(4 * (.58 - .45 - .02475 - .02436))
        assert report_text(report).count("slower+thinner") == 5


def test_missed_entry_cannot_sell_and_later_quote_never_leaks_back(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    intent(tape, "A", 1100)
    quote(tape, 1101, .47, .48)
    quote(tape, 1103, .43, .44)
    intent(tape, "A", 1200, "sell")
    quote(tape, 1201, .60, .61)
    quote(tape, 1204, .60, .61)
    tape.record_settlement(Settlement(WINDOW.slug, 0, 80, 79.9), 1905)
    rows = compare(tape.path, 0, 2000)["models"]["A"]["scenarios"]
    assert rows["baseline"]["entry"]["fill_rate"] == 0
    assert rows["baseline"]["exit"]["attempts"] == 0 and rows["baseline"]["net_pnl"] == 0
    assert rows["slower"]["entry"]["fill_rate"] == 1 and rows["slower"]["exit"]["attempts"] == 1


def test_common_pnl_excludes_missing_books_or_unknown_official_payout(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    intent(tape, "A", 1100)
    quote(tape, 1101, .39, .40)
    tape.append(WINDOW.slug, 1103, "rest", "gap", {})
    tape.record_settlement(Settlement(WINDOW.slug, 1, 80, 80.1), 2010)
    rows = compare(tape.path, 0, 2000)["models"]["A"]["scenarios"]
    assert rows["slower"]["entry"]["unverified"] == 1
    assert rows["baseline"]["entry"]["fill_rate"] == 1
    assert all(row["net_pnl"] is None for row in rows.values())

    tape.append(WINDOW.slug, 1104, "rest", "book",
                asdict(Book(((.39, 20),), ((.40, 20),), 1104)))
    rows = compare(tape.path, 0, 2000)["models"]["A"]["scenarios"]
    assert all(row["net_pnl"] is None for row in rows.values())
    rows = compare(tape.path, 0, 2020)["models"]["A"]["scenarios"]
    assert rows["baseline"]["net_pnl"] == pytest.approx(10 * (1 - .40 - .024))


def test_exit_replay_cannot_resell_an_unchanged_bid_or_execute_at_close(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    tape.record_window(WINDOW)
    intent(tape, "A", 1100)
    quote(tape, 1101, .39, .40)
    quote(tape, 1104, .39, .40)
    for when in (1200, 1210, 1898):
        intent(tape, "A", when, "sell")
        quote(tape, when + 1, .60, .61, size=4)
        quote(tape, when + 2, .60, .61, size=4)
    tape.record_settlement(Settlement(WINDOW.slug, 0, 80, 79.9), 1905)
    baseline = compare(tape.path, 0, 2000)["models"]["A"]["scenarios"]["baseline"]
    assert baseline["exit"]["attempts"] == 3
    assert baseline["exit"]["filled_contracts"] == 2
    assert baseline["net_pnl"] == pytest.approx(2 * (.60 - .024) - 10 * (.40 + .024))


def test_stream_trade_invalidates_older_book_until_exchange_updates():
    book = asdict(Book(((.40, 10),), ((.42, 10),), 100, exchange_at=99))
    cursor = BookCursor([(100, "book", book), (101, "trade", {"exchange_at": 100.5}),
                         (102, "book", {**book, "at": 102, "exchange_at": 101})])
    assert cursor.advance(100) == "book"
    assert cursor.advance(101) == "gap"
    assert cursor.advance(102) == "book"
    assert cursor.advance(108) == "gap"


def test_market_stream_authentication_and_normalization():
    key = Ed25519PrivateKey.generate()
    headers = stream_headers("example-key", key, 100.5)
    key.public_key().verify(b64decode(headers["X-PM-Signature"]), f"100500GET{WS_PATH}".encode())
    assert headers["X-PM-Access-Key"] == "example-key"
    raw = {"marketData": {"marketSlug": WINDOW.slug, "state": "MARKET_STATE_OPEN",
           "bids": [{"px": {"value": ".5"}, "qty": "10"}], "offers": [{"px": {"value": ".52"}, "qty": "4"}]}}
    row = stream_row(raw, WINDOW.slug, 100)
    assert row[:4] == (WINDOW.slug, 100, "stream", "book") and row[4]["yes_bids"] == ((.5, 10),)
    assert stream_row(raw, "different-market", 100) is None
    trade = {"trade": {"marketSlug": WINDOW.slug, "price": {"value": ".52"}, "quantity": "1",
                       "tradeTime": "1970-01-01T00:01:40Z"}}
    assert stream_row(trade, WINDOW.slug, 101)[4]["exchange_at"] == 100


def test_new_install_report_has_all_models_and_no_fabricated_pnl(tmp_path):
    report = compare(Path(tmp_path, "empty.sqlite3"), 0, 100)
    assert report["markets_recorded"] == 0
    assert set(report["models"]) == set("ABCDE")
    assert report["models"]["D"]["scenarios"]["baseline"]["net_pnl"] is None
