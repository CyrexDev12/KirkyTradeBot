from dataclasses import asdict
from decimal import Decimal

import pytest

from backend.updown.feed import Settlement
from backend.updown.strategy import Book, Window
from backend.wti.live_store import LiveStore
from backend.wti.paper_execution_replay import load_orders, report
from backend.wti.tape import Tape


SLUG = "cpwtif-wti-updown-15m-1970-01-01-0015z"
START, END = 900, 1800
SEND = 1000.0


def history(directory, source="stream", filled="2", *, acknowledged=True):
    live = LiveStore(directory / "live.sqlite3")
    tape = Tape(directory / "tape.sqlite3")
    tape.record_window(Window("wti", SLUG, START, END, 80, "MARKET_STATUS_OPEN", .095,
                              minimum_quantity=".01", price_tick=".01"))
    sid = live.record_signal(SLUG, START, END, SEND - 2,
                             {"side": "UP", "ready_at": SEND - 1, "deadline": SEND + 4,
                              "signal_source": "stream" if source == "stream" else "paper"})
    entry = live.claim_entry(sid, "entry", SEND - .4, {"side": "UP", "quantity": "2", "price": ".40",
                                                       "quote_source": source})
    assert live.authorize_entry(entry["id"], SEND - .1)
    live.record_entry_timing(SEND + .21, {"slug": SLUG, "signal_at": SEND - 2, "submit_started_at": SEND,
                                          "max_exchange_age_seconds": 15,
                                          "book_mode": "stream_end_to_end" if source == "stream" else "rest"})
    if acknowledged:
        live.update_order(entry["id"], "ORDER_STATE_FILLED" if filled == "2" else "ORDER_STATE_CANCELED",
                          SEND + .21, venue_id="venue-entry", payload={"filled": filled,
                          **({"average_price": ".40", "fees": ".04"} if filled != "0" else {})})
        if filled != "0":
            live.record_fill(entry["id"], "entry-fill", SEND + .13, filled, ".40", ".04",
                             {"exchange_at": SEND + .13, "observed_at": SEND + .21})
    return live, tape, entry


def quote(tape, at, sequence, *, bid=.38, ask=.39, size=20, source="stream"):
    tape.append(SLUG, at, source, "book", asdict(Book(((bid, size),), ((ask, size),), at, at - .02,
                                                    at - .05 if source == "rest" else None, sequence)))


@pytest.mark.parametrize(("actual_qty", "shown", "actual", "modeled"),
                         [("2", 20, "filled", "filled"), ("1", 1, "partial", "partial"),
                          ("0", 0, "no_fill", "no_fill")])
def test_replay_uses_trade_cohort_exact_quantity_limit_and_arrival(tmp_path, actual_qty, shown, actual, modeled):
    live, tape, entry = history(tmp_path, filled=actual_qty)
    quote(tape, SEND + .1, 1, ask=.41 if not shown else .39, size=shown or 1)
    quote(tape, SEND + .2, 2, ask=.20)

    result = report(live.path, tape.path, START, END, include_orders=True, trade_ids={entry["trade_id"]})
    order = result["order_details"][0]
    assert result["order_kinds"]["entry"]["paired_outcomes"] == {f"{actual}/{modeled}": 1}
    assert order["sent_at"] == SEND and order["modeled_arrival_at"] == SEND + .15
    assert order["requested_quantity"] == 2 and order["actual_quantity"] == order["modeled_quantity"] == Decimal(actual_qty)
    if shown:
        assert order["modeled_price"] == Decimal(".39")
        assert order["observed_send_to_first_fill"] == pytest.approx(.13)
        assert result["order_kinds"]["entry"]["mean_modeled_adverse_price_error_cents"] == -1
        assert result["order_kinds"]["entry"]["quantity_weighted_absolute_price_error_cents"] == 1
    else:
        assert order["modeled_price"] is None and order["actual_fee"] == 0
    assert report(live.path, tape.path, START, END, trade_ids={42})["orders"] == 0


@pytest.mark.parametrize(("source", "frames", "reason"), [
    ("direct_rest", [(SEND + .1, "rest")], "direct_quote_unrecorded"),
    ("stream", [(SEND + .2, "stream")], "book_unavailable"),
    ("stream", [(SEND + .1, "rest")], "book_unavailable"),
    ("stream", [(SEND - 1.5, "stream")], "book_unavailable"),
])
def test_replay_reports_missing_source_without_making_up_a_trade(tmp_path, source, frames, reason):
    live, tape, _ = history(tmp_path, source=source)
    for at, kind in frames:
        quote(tape, at, 1, source=kind)
    result = report(live.path, tape.path, START, END, include_orders=True)
    order = result["order_details"][0]
    assert order["reason"] == reason and order["modeled_quantity"] is None
    assert result["order_kinds"]["entry"]["matched_orders"] == 0


def test_as_of_order_state_uses_events_and_no_later_actual_fill(tmp_path):
    live, _, _ = history(tmp_path)
    before_confirmation = load_orders(live.path, START, SEND + .2)
    assert len(before_confirmation) == 1
    assert before_confirmation[0].actual_state == "sending"
    assert before_confirmation[0].actual_quantity is before_confirmation[0].first_fill_at is None


def add_target(live, trade, cancel_at, *, venue_state="ORDER_STATE_CANCELED"):
    target = live.prepare_order(trade, "target", "target", SEND + .9,
                                {"side": "UP", "quantity": "2", "price": ".70", "expires_at": END - 5})
    live.update_order(target["id"], "sending", SEND + 1)
    live.update_order(target["id"], venue_state, cancel_at + 1, venue_id="venue-target",
                      payload={"filled": "0", "cancel_requested_at": cancel_at})


def test_replay_target_stops_at_cancel_request_not_later_quote_or_receipt(tmp_path):
    live, tape, entry = history(tmp_path)
    quote(tape, SEND + .1, 1)
    quote(tape, SEND + 1.2, 2)
    add_target(live, entry["trade_id"], SEND + 1.3)
    quote(tape, SEND + 2.4, 3, bid=.90, ask=.91)
    tape.record_settlement(Settlement(SLUG, 1, 80, 81), END + 1)
    live.close_trade(entry["trade_id"], END + 2, "CLOSED", {"net_pnl": "1.16", "fees_exact": True})

    result = report(live.path, tape.path, START, END + 10, include_orders=True)
    target = result["order_details"][1]
    assert target["kind"] == "target" and target["resting_target_proxy"] and target["quote_source_inferred"]
    assert target["actual_outcome"] == target["modeled_outcome"] == "no_fill"
    assert result["fixed_real_entry_pnl"] == {"matched_closed_trades": 1, "using_quote_proxies": 1,
                                              "actual_net": Decimal("1.16"), "modeled_exit_net": Decimal("1.16")}


def test_crossing_during_cancellation_is_unverified(tmp_path):
    live, tape, entry = history(tmp_path)
    quote(tape, SEND + .1, 1)
    quote(tape, SEND + 1.2, 2)
    add_target(live, entry["trade_id"], SEND + 1.3)
    quote(tape, SEND + 1.4, 3, bid=.9, ask=.91)

    result = report(live.path, tape.path, START, END, include_orders=True)
    assert result["order_details"][1]["reason"] == "target_fill_possible_during_cancel"
    assert result["order_details"][1]["modeled_quantity"] is None


def test_target_gap_is_unverified_and_excluded_from_paired_pnl(tmp_path):
    live, tape, entry = history(tmp_path)
    quote(tape, SEND + .1, 1)
    quote(tape, SEND + 1.2, 2)
    add_target(live, entry["trade_id"], SEND + 30)
    tape.record_settlement(Settlement(SLUG, 1, 80, 81), END + 1)
    live.close_trade(entry["trade_id"], END + 2, "CLOSED", {"net_pnl": "1.16", "fees_exact": True})

    result = report(live.path, tape.path, START, END + 10, include_orders=True)
    target = result["order_details"][1]
    assert target["modeled_quantity"] is None and target["reason"] == "extended_unobserved_target_quotes"
    assert result["fixed_real_entry_pnl"]["matched_closed_trades"] == 0
