from dataclasses import asdict
from decimal import Decimal

from backend.updown.feed import Settlement
from backend.updown.strategy import Book, Window
from backend.wti.live_exit_replay import report
from backend.wti.live_store import LiveStore
from backend.wti.tape import Tape


def test_exit_replay_separates_hold_trigger_and_actual_book_price(tmp_path):
    slug = "cpwtif-wti-updown-15m-1970-01-01-0015z"
    journal = tmp_path / "live.sqlite3"
    tape_path = tmp_path / "tape.sqlite3"
    live, tape = LiveStore(journal), Tape(tape_path)
    signal_id = live.record_signal(slug, 900, 1800, 1000, {"signal_source": "stream", "side": "UP"})
    entry = live.claim_entry(signal_id, "entry", 1002, {"side": "UP", "quantity": "2", "price": "0.40"})
    assert live.authorize_entry(entry["id"], 1002)
    live.update_order(entry["id"], "ORDER_STATE_FILLED", 1003, venue_id="entry-1",
                      payload={"filled": "2", "average_price": "0.40", "fees": "0.04"})
    stop = live.prepare_order(entry["trade_id"], "stop_loss", "stop", 1103,
                              {"side": "UP", "quantity": "2", "price": "0.16"})
    live.update_order(stop["id"], "sending", 1103)
    live.update_order(stop["id"], "ORDER_STATE_FILLED", 1104, venue_id="stop-1",
                      payload={"filled": "2", "average_price": "0.17", "fees": "0.02"})
    live.close_trade(entry["trade_id"], 1104, "CLOSED", {"net_pnl": "-0.52", "fees": "0.06",
                     "fees_exact": True, "payout": "0", "exit_reason": "stop_loss"})
    tape.record_window(Window("wti", slug, 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point",
                              price_tick="0.01", minimum_quantity="0.01"))
    tape.append(slug, 1102, "stream", "book", asdict(Book(((.18, 10),), ((.20, 10),), 1102, 1101.8)))
    tape.append(slug, 1103.5, "stream", "book", asdict(Book(((.05, 10),), ((.20, 10),), 1103.5, 1103.4)))
    tape.record_settlement(Settlement(slug, 1, 80, 81), 1805)
    cohort = {entry["trade_id"]: {"slug": slug, "side": "UP", "signal_at": 1000, "quantity": "2", "price": ".4"}}
    result = report(journal, tape_path, 1800, 1900, stop_fraction=Decimal("0.5"), cohort=cohort)
    assert result["trades"] == result["expected_trades"] == 1
    assert result["actual_net"] == Decimal("-.52")
    assert result["hold_comparison"]["hold_net"] == Decimal("1.16")
    assert result["stopped_trade_hold_comparison"]["stopped_but_profitable_at_settlement"] == 1
    stops = result["stop_execution_comparison"]
    assert stops["book_vs_actual_matched"] == 1
    assert stops["actual_minus_book_gross"] == Decimal("-.02")
    assert stops["book_minus_trigger_gross_on_common_legs"] == Decimal("-.04")
    assert stops["actual_minus_trigger_gross"] == Decimal("-.06")
    assert result["stop_details"][0]["book_at"] == 1102

    before_settlement = report(journal, tape_path, 900, 1750, stop_fraction=Decimal("0.5"))
    assert before_settlement["trades"] == 1 and before_settlement["hold_comparison"]["matched"] == 0
    public = report(journal, tape_path, 900, 1801, stop_fraction=Decimal("0.5"),
                    official_payouts={slug: Decimal(1)})
    assert public["hold_comparison"]["hold_net"] == Decimal("1.16")
    assert public["details"][0]["settlement_source"] == "verified_public_payout"
    inconsistent = report(journal, tape_path, 900, 1900, stop_fraction=Decimal("0.5"),
                          official_payouts={slug: Decimal(0)})
    assert inconsistent["hold_comparison"]["matched"] == 0
    assert inconsistent["details"][0]["settlement_source"] == "conflicting_official_payouts"
