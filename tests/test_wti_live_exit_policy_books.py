from dataclasses import asdict, replace
from decimal import Decimal
from math import nextafter
from types import SimpleNamespace

from backend.updown.strategy import Book, Window
from backend.wti.live_exit_policy_books import BookDepth, exit_timeline, load_exit_books
from backend.wti.tape import Tape, tape_path


WINDOW = Window("wti", "wti-oil-15m", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point")


def book(at, *, venue_at=None, bids=((.48, 4), (.45, 3)), offers=((.55, 8),), seq=None):
    return Book(bids, offers, at, at if venue_at is None else venue_at, sequence=seq)


def test_load_exit_books_filters_market_source_cutoff_and_uses_recorded_order(tmp_path):
    tape = Tape(tape_path(tmp_path))
    tape.record_window(WINDOW)
    tape.append(WINDOW.slug, 899, "stream", "book", asdict(book(899)))
    tape.append(WINDOW.slug, 1001, "rest", "book", asdict(book(1001)))
    tape.append("another-market", 1002, "stream", "book", asdict(book(1002)))
    tape.append(WINDOW.slug, 1004, "stream", "book", asdict(book(1004, seq=10)))
    tape.append(WINDOW.slug, 1004, "stream", "trade", {"exchange_at": 1004.1})
    tape.append(WINDOW.slug, 1005, "stream", "book", asdict(book(1005, seq=11)))
    tape.append(WINDOW.slug, 1800, "stream", "book", asdict(book(1800)))
    trade = SimpleNamespace(slug=WINDOW.slug, window_start=WINDOW.start, window_end=WINDOW.end)
    window, frames = load_exit_books(tape.path, trade, 1004)
    assert window == WINDOW and [(at, kind) for at, kind, _ in frames] == [(1004, "book"), (1004, "trade")]
    assert list(exit_timeline(window, frames, as_of=1004)) == [(1004, None, False)]
    mapping = {"market_slug": WINDOW.slug, "window_start": WINDOW.start, "window_end": WINDOW.end}
    assert [at for at, _, _ in load_exit_books(tape.path, mapping, 1900)[1]] == [1004, 1004, 1005]
    assert load_exit_books(tape.path, {**mapping, "window_end": 1900}, 1900) == (None, [])


def test_timeline_refuses_stale_books_and_emits_exact_expiry_for_sparse_updates():
    frames = [
        (1000, "book", asdict(book(1000, venue_at=999, seq=1))),
        (1006, "book", asdict(book(1006, venue_at=1002, seq=2))),
        (1007.5, "book", asdict(book(1007.5, venue_at=1002, seq=3))),
        (1008, "book", asdict(book(1008, venue_at=1007, seq=4))),
    ]
    rows = list(exit_timeline(WINDOW, frames, as_of=1009))
    assert rows[0] == (1000, book(1000, venue_at=999, seq=1), False)
    assert rows[1] == (nextafter(1003, float("inf")), None, False)
    assert rows[2] == (1006, book(1006, venue_at=1002, seq=2), False)
    assert rows[3] == (nextafter(1007, float("inf")), None, False)
    assert rows[4] == (1007.5, None, False)  # 5.5 seconds old at the exchange.
    assert rows[5] == (1008, book(1008, venue_at=1007, seq=4), False)


def test_timeline_keeps_halt_and_trade_invalidation_until_fresh_full_recovery():
    frames = [
        (1000, "book", asdict(book(1000, seq=7))),
        (1000.25, "closed", {"exchange_at": 1000.25}),
        (1000.5, "gap", {}),
        (1000.75, "book", asdict(book(1000.75, venue_at=1000.25, seq=8))),
        (1001, "book", asdict(book(1001, seq=9))),
        (1001.25, "trade", {"exchange_at": 1001.15}),
        (1001.5, "book", asdict(book(1001.5, venue_at=1001.1, seq=10))),
        (1001.75, "book", asdict(book(1001.75, venue_at=1001.7, seq=11))),
    ]
    rows = list(exit_timeline(WINDOW, frames, as_of=1002))
    assert [(at, quote is not None, halt) for at, quote, halt in rows] == [
        (1000, True, False), (1000.25, False, True), (1000.5, False, True), (1000.75, False, True),
        (1001, True, False), (1001.25, False, False), (1001.5, False, False), (1001.75, True, False),
    ]


def test_depth_can_fill_fractional_parts_without_reusing_consumed_bids():
    depth = BookDepth(quantity_step=".01")
    initial = book(1000)
    first = depth.take(initial, "UP", "2.00", ".45")
    assert first.quantity == Decimal("1.75")
    assert first.levels == ((Decimal(".48"), Decimal("1.00")), (Decimal(".45"), Decimal(".75")))
    assert first.gross_proceeds == Decimal(".8175")
    assert first.average_price == first.gross_proceeds / first.quantity
    assert depth.take(initial, "UP", ".25", ".45") is None
    assert depth.take(replace(initial, at=1001), "UP", ".25", ".45") is None
    assert depth.take(book(1001), "UP", ".25", ".45") is None
    depth.observe(None)  # A gap does not replenish the spent levels.
    assert depth.take(book(1002), "UP", ".25", ".45") is None
    enlarged = book(1003, bids=((.48, 5.07), (.45, 3)))
    later = depth.take(enlarged, "UP", "1", ".46")
    assert later.quantity == Decimal(".26") and later.average_price == Decimal(".48")
    assert depth.take(book(1004, bids=((.48, 5.07),)), "UP", ".10") is None


def test_depth_down_bids_use_same_instruments_complement_and_respect_limit():
    depth = BookDepth(quantity_step=".01")
    initial = book(1000, offers=((.52, 3), (.54, 8)))
    assert depth.take(initial, "DOWN", "1", ".49") is None
    result = depth.take(initial, "DOWN", "1", ".47")
    assert result.levels == ((Decimal(".48"), Decimal(".75")),)
    assert result.gross_proceeds == Decimal(".3600")
