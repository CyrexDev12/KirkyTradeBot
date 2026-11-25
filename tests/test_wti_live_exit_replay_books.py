from dataclasses import asdict, replace
from decimal import Decimal

from backend.updown.strategy import Book, Window
from backend.wti.live_exit_replay_books import ExitLeg, benchmark_exit_books
from backend.wti.tape import Tape, tape_path


WINDOW = Window("wti", "oil-15m", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point",
                minimum_quantity="0.01", price_tick="0.01")


def leg(at, quantity="1.00", limit=".35", **fields):
    return replace(ExitLeg(WINDOW.slug, WINDOW.start, WINDOW.end, at, "UP", Decimal(quantity), Decimal(limit)),
                   **fields)


def tape_with_market(directory):
    tape = Tape(tape_path(directory))
    tape.record_window(WINDOW)
    return tape


def frame(tape, at, *, venue_at=None, bids=((.44, 3), (.40, 2), (.30, 4)), asks=((.50, 3),),
          seq=None, source="stream", slug=WINDOW.slug):
    quote = Book(bids, asks, at, at if venue_at is None else venue_at, sequence=seq)
    tape.append(slug, at, source, "book", asdict(quote))


def test_exit_sweeps_only_visible_fractional_depth_at_or_above_actual_limit(tmp_path):
    tape = tape_with_market(tmp_path)
    frame(tape, 1100)
    result, = benchmark_exit_books(tape.path, [leg(1101, fee_coefficient=Decimal(".095"))])
    assert result.reason == "eligible"
    assert result.book_at == 1100 and result.book_exchange_at == 1100
    assert result.best_bid == Decimal(".44")
    assert result.total_visible_depth == 9 and result.visible_depth == 5
    assert result.levels == ((Decimal(".44"), Decimal(".75")), (Decimal(".40"), Decimal(".25")))
    assert result.average_price == result.gross_proceeds == Decimal(".4300")
    assert result.modeled_fee == Decimal(".02") and result.net_proceeds == Decimal(".4100")


def test_shadow_benchmark_rejects_insufficient_and_worse_prices_without_consuming(tmp_path):
    tape = tape_with_market(tmp_path)
    frame(tape, 1100, bids=((.45, 3), (.29, 200)))
    priced, shallow, filled, = benchmark_exit_books(tape.path, [
        leg(1101, limit=".46"), leg(1101, quantity="1"), leg(1101, quantity=".50"),
    ])
    assert priced.reason == "price_below_limit" and priced.best_bid == Decimal(".45")
    assert priced.visible_depth == 0 and priced.total_visible_depth == 203
    assert shallow.reason == "insufficient_depth" and shallow.executable_quantity == Decimal(".75")
    assert shallow.average_price is None and shallow.net_proceeds is None
    assert filled.reason == "eligible" and filled.executable_quantity == Decimal(".50")


def test_exit_uses_down_complement_and_only_observed_liquidity_replenishment(tmp_path):
    tape = tape_with_market(tmp_path)
    frame(tape, 1100, asks=((.55, 4),))
    frame(tape, 1101, asks=((.55, 4),))
    tape.append(WINDOW.slug, 1102, "stream", "gap", {})
    frame(tape, 1103, asks=((.55, 4),))
    frame(tape, 1104, asks=((.55, 6),))
    until = [leg(at, quantity=".50", limit=".44", side="DOWN") for at in (1100, 1100.2, 1101, 1102, 1103, 1104)]
    results = benchmark_exit_books(tape.path, until)
    assert [r.reason for r in results] == [
        "eligible", "insufficient_depth", "insufficient_depth", "stream_gap", "insufficient_depth", "eligible",
    ]
    assert results[0].average_price == results[-1].average_price == Decimal(".45")
    assert results[-1].executable_quantity == Decimal(".50")


def test_benchmark_preserves_original_snapshot_timestamps_and_cannot_look_ahead(tmp_path):
    tape = tape_with_market(tmp_path)
    frame(tape, 1100, venue_at=1094)
    frame(tape, 1110, seq=1, source="rest")
    frame(tape, 1110, seq=1, slug="unrelated")
    frame(tape, 1111, venue_at=1110, seq=2)
    tape.append(WINDOW.slug, 1115.01, "stream", "trade", {"exchange_at": 1112})
    frame(tape, 1116, venue_at=1111, seq=3)
    test = [leg(t) for t in (1099, 1100, 1110, 1111, 1115, 1115.01, 1116)]
    results = benchmark_exit_books(tape.path, test)
    assert [r.reason for r in results] == [
        "no_stream_book", "stale_book", "stale_book", "eligible", "stale_book", "invalidated_book", "invalidated_book",
    ]
    assert results[4].book_at == 1111 and results[4].book_exchange_at == 1110
    assert benchmark_exit_books(tape.path, [leg(1110)], wait_seconds=1)[0].reason == "eligible"
    assert benchmark_exit_books(tape.path, [leg(1110)], wait_seconds=1, as_of=1110.5)[0].reason == "probe_after_cutoff"


def test_exit_benchmark_persists_halt_and_rejects_wrong_window_or_after_close(tmp_path):
    tape = tape_with_market(tmp_path)
    frame(tape, 1100, seq=1)
    tape.append(WINDOW.slug, 1101, "stream", "closed", {"exchange_at": 1101})
    tape.append(WINDOW.slug, 1101.25, "stream", "gap", {})
    frame(tape, 1102, venue_at=1101, seq=2)
    frame(tape, 1103, venue_at=1102, seq=3)
    probes = [leg(1101), leg(1101.25), leg(1102), leg(1103),
              leg(1103, window_start=901), leg(1800), leg(1103, slug="missing")]
    assert [q.reason for q in benchmark_exit_books(tape.path, probes)] == [
        "market_not_open", "market_not_open", "market_not_open", "eligible", "window_mismatch",
        "outside_window", "market_not_recorded",
    ]
