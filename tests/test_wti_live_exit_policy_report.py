from dataclasses import asdict
from decimal import Decimal

from backend.updown.feed import Settlement
from backend.updown.strategy import Book, Window
from backend.wti.live_exit_policy_replay import report
from backend.wti.live_store import LiveStore
from backend.wti.tape import Tape


SLUG = "cpwtif-wti-updown-15m-1970-01-01-0015z"


def recorded_trade(directory):
    live = LiveStore(directory / "live.sqlite3")
    tape = Tape(directory / "tape.sqlite3")
    signal_at = 1700
    signal = live.record_signal(SLUG, 900, 1800, signal_at,
                                {"side": "UP", "signal_source": "stream", "ready_at": 1702, "deadline": 1710})
    entry = live.claim_entry(signal, "entry", 1702, {"slug": SLUG, "side": "UP", "quantity": "2.00",
                             "price": ".40", "signal_source": "stream", "quote_source": "stream"})
    assert live.authorize_entry(entry["id"], 1702.1)
    live.update_order(entry["id"], "ORDER_STATE_FILLED", 1702.5, venue_id="market-entry",
                      payload={"filled": "2.00", "average_price": ".40", "fees": ".04"})
    live.record_fill(entry["id"], "entry-fill", 1702.4, "2.00", ".40", ".04",
                     {"exchange_at": 1702.4, "observed_at": 1702.5})
    target = live.prepare_order(entry["trade_id"], "target", "target", 1703, {"slug": SLUG, "side": "UP",
                                "quantity": "2.00", "price": ".70", "expires_at": 1795})
    live.update_order(target["id"], "sending", 1703.1)
    live.update_order(target["id"], "ORDER_STATE_CANCELED", 1710.2, venue_id="market-target",
                      payload={"filled": "0", "cancel_requested_at": 1710.1})
    stop = live.prepare_order(entry["trade_id"], "stop_loss", "stop", 1710.3,
                              {"slug": SLUG, "side": "UP", "quantity": "2.00", "price": ".17"})
    live.update_order(stop["id"], "sending", 1710.35)
    live.update_order(stop["id"], "ORDER_STATE_FILLED", 1710.6, venue_id="market-stop",
                      payload={"filled": "2.00", "average_price": ".18", "fees": ".02"})
    live.record_fill(stop["id"], "stop-fill", 1710.5, "2.00", ".18", ".02",
                     {"exchange_at": 1710.5, "observed_at": 1710.6})
    live.close_trade(entry["trade_id"], 1710.75, "CLOSED", {"net_pnl": "-.50", "fees": ".06",
                     "fees_exact": True, "payout": "0", "exit_reason": "stop_loss"})
    tape.record_window(Window("wti", SLUG, 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point",
                              minimum_quantity=".01", price_tick=".01"))
    return live, tape, entry["trade_id"]


def book_row(at, bid, sequence, quantity=20):
    book = Book(((bid, quantity),), ((round(bid + .05, 2), 20),), at, at - .05, sequence=sequence)
    return SLUG, at, "stream", "book", asdict(book)


def test_policy_replay_keeps_actual_entry_and_filters_a_transient_stop(tmp_path):
    live, tape, trade_id = recorded_trade(tmp_path)
    rows = []
    for step in range(30):
        at = 1702.5 + step * .5
        bid = .19 if 1710 <= at < 1711 else .72 if at >= 1715 else .39
        rows.append(book_row(at, bid, step + 1))
    tape.append_many(rows)
    tape.record_settlement(Settlement(SLUG, 1, 80, 81), 1805)
    cohort = {
        trade_id: {"slug": SLUG, "side": "UP", "signal_at": 1700, "quantity": "2.00", "price": ".40"},
        999: {"slug": "missing-market", "side": "UP", "signal_at": 1700, "quantity": "1", "price": ".42"},
    }

    result = report(live.path, tape.path, 900, 1900, cohort=cohort)
    assert result["trades"] == result["common_trades"] == 1
    assert result["expected_trades"] == 2 and result["missing_or_mismatched_trade_ids"] == [999]
    assert result["actual_net"] == result["actual_net_common"] == Decimal("-.50")
    assert result["pure_hold_trades"] == 1 and result["pure_hold_net"] == Decimal("1.16")
    assert result["details"][0]["quantity"] == Decimal("2.00")
    scenarios = result["scenario_totals"]
    immediate, confirmed, target_only = (scenarios[key] for key in
                                         ("immediate_stop", "confirmed_stop", "target_then_settlement"))
    assert immediate["stop_trades"] == 1 and immediate["losses"] == 1
    assert confirmed["stop_trades"] == 0 and confirmed["target_trades"] == confirmed["wins"] == 1
    assert target_only["target_trades"] == target_only["wins"] == 1
    assert target_only["net"] == confirmed["net"]
    assert immediate["net"] < 0 < confirmed["net"] < result["pure_hold_net"]


def test_policy_replay_does_not_make_unobserved_exits_match(tmp_path):
    live, tape, _ = recorded_trade(tmp_path)
    tape.append_many((book_row(1702.5, .39, 1), (SLUG, 1703, "stream", "gap", {})))
    tape.record_settlement(Settlement(SLUG, 1, 80, 81), 1805)

    result = report(live.path, tape.path, 900, 1900)
    assert result["trades"] == 1 and result["common_trades"] == 0
    assert result["pure_hold_trades"] == 1 and result["pure_hold_net"] == Decimal("1.16")
    assert result["details"][0]["unverified_reason"] == "extended_unobserved_quotes"

    report_before_close = report(live.path, tape.path, 900, 1799)
    assert report_before_close["trades"] == 1 and report_before_close["common_trades"] == 0
    assert report_before_close["pure_hold_trades"] == 0
    assert report_before_close["details"][0]["unverified_reason"] == "window_still_open"


def test_replay_partial_target_settles_only_the_unsold_quantity(tmp_path):
    live, tape, _ = recorded_trade(tmp_path)
    at_target = 1715
    rows = []
    for step in range(185):
        at = 1702.5 + step * .5
        rows.append(book_row(at, .72, step + 1, quantity=4) if at == at_target else book_row(at, .39, step + 1))
    tape.append_many(rows)
    tape.record_settlement(Settlement(SLUG, 1, 80, 81), 1805)

    result = report(live.path, tape.path, 900, 1900)
    assert result["common_trades"] == 1
    case = result["details"][0]["scenarios"]["target_then_settlement"]
    assert case["target_quantity"] == case["settled_quantity"] == Decimal("1.00")
    assert case["stop_quantity"] == 0 and case["settlement_proceeds"] == Decimal("1.00")
    assert case["net_pnl"] == case["target_gross"] + Decimal(1) - case["exit_fees"] - Decimal(".84")
