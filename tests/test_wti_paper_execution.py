from decimal import Decimal
import json

from backend.updown.strategy import Book
from backend.wti.paper_execution import PaperExecution, PaperExits


D = Decimal


def book(at, bids=((.48, 20),), offers=((.50, 20),), *, exchange_at=None, sequence=None):
    return Book(bids, offers, at, at if exchange_at is None else exchange_at, sequence=sequence)


def ioc(execution, quote, *, side="UP", action="buy", size="1", limit=".50", at=100, step=".25", **kwargs):
    return execution.match_ioc(quote, side=side, action=action, quantity=size, limit=limit, arrival_at=at,
                               cutoff=110, quantity_step=step, fee_coefficient=".095", **kwargs)


def test_ioc_partial_executes_price_levels_once_and_resumes_ledger():
    execution = PaperExecution()
    offers = ((.49, 1.25), (.50, 2.25), (.51, 8))
    first = ioc(execution, book(100, offers=offers, sequence=1), size="6")
    assert (first.outcome, first.quantity, first.gross, first.fee) == ("partial", D("3.50"), D("1.7375"), D(".08"))
    assert first.levels == ((D(".49"), D("1.25")), (D(".50"), D("2.25")))
    assert first.average_price == first.gross / first.quantity

    recovered = PaperExecution(state=json.loads(json.dumps(execution.dump())))
    duplicate = book(101, offers=offers, exchange_at=100, sequence=1)
    assert ioc(recovered, duplicate, at=101).reason == "repeated_book_frame"
    unchanged = book(102, offers=offers, sequence=2)
    assert ioc(recovered, unchanged, at=102).outcome == "no_fill"
    replenished = book(103, offers=((.49, 1.25), (.50, 2.75), (.51, 8)), sequence=3)
    assert ioc(recovered, replenished, at=103).quantity == D(".50")
    assert ioc(recovered, book(104, offers=offers, sequence=1), at=104).reason == "book_out_of_order"


def test_ioc_shares_underlying_yes_bid_depth_for_equivalent_up_and_down_actions():
    execution = PaperExecution()
    quote = book(100, bids=((.53, 3),), offers=((.54, 6),))
    down_buy = ioc(execution, quote, side="DOWN", size="2", limit=".47")
    assert (down_buy.outcome, down_buy.average_price) == ("filled", D(".47"))
    up_exit = ioc(execution, quote, action="sell", size="2", limit=".53")
    assert (up_exit.outcome, up_exit.quantity, up_exit.average_price) == ("partial", D("1"), D(".53"))
    buy_up = ioc(execution, quote, size="2", limit=".54")
    assert buy_up.outcome == "filled"
    sell_down = ioc(execution, quote, side="DOWN", action="sell", size="6", limit=".46")
    assert (sell_down.outcome, sell_down.quantity, sell_down.average_price) == ("partial", D("4"), D(".46"))


def test_ioc_freshness_original_limit_and_no_second_preflight_haircut():
    thin = book(100, offers=((.50, 1),))
    assert ioc(PaperExecution(), thin, step=1).outcome == "filled"
    assert ioc(PaperExecution(depth_share=".25"), thin, step=1).outcome == "no_fill"
    assert ioc(PaperExecution(), thin, limit=".49").outcome == "no_fill"
    assert ioc(PaperExecution(), thin, minimum_quote_at=100.1).reason == "book_before_ready"
    assert ioc(PaperExecution(), book(101)).reason == "book_stale_or_future"
    assert ioc(PaperExecution(), book(96)).reason == "book_stale_or_future"
    assert ioc(PaperExecution(), book(100, exchange_at=90)).outcome == "filled"
    assert ioc(PaperExecution(), book(100, exchange_at=90), action="sell").outcome == "unverified"
    assert ioc(PaperExecution(), thin, market_open=False).reason == "market_not_open"
    assert ioc(PaperExecution(), None).reason == "book_unavailable"
    assert ioc(PaperExecution(), book(109), at=110).outcome == "expired"


def start_exits(execution, **kwargs):
    return PaperExits(execution, side=kwargs.pop("side", "UP"), quantity="2", entry_price=".50", entry_at=1000,
                      window_end=kwargs.pop("window_end", 1015), quantity_step=".50", price_tick=".01",
                      fee_coefficient=".095", target_latency=kwargs.pop("target_latency", .2),
                      cancel_latency=.4, stop_latency=.2, decision_interval=kwargs.pop("decision_interval", 0), **kwargs)


def test_exit_resting_target_cancel_ack_stop_floor_partial_fill_and_official_residual():
    execution = PaperExecution()
    position = start_exits(execution)
    initial = book(1000, bids=((.75, 20),), offers=((.80, 20),))
    submitted, = position.advance(1000, initial)
    assert (submitted.kind, submitted.limit) == ("target_submitted", D(".75"))
    working, = position.advance(1000.2, initial)
    assert working.kind == "target_working"
    assert position.remaining == 2  # Replay only credits target bids observed after activation.
    target, = position.advance(1001, book(1001, bids=((.76, 4),), offers=((.80, 20),)))
    assert (target.kind, target.quantity, target.proceeds) == ("target_fill", D("1"), D(".75"))

    triggered, cancelling = position.advance(1002, book(1002, bids=((.24, 4),)))
    assert triggered.kind == "stop_triggered" and cancelling.kind == "target_cancel_requested"
    assert position.next_action_at == 1002.4
    assert position.advance(1002.25, None) == ()

    execution = PaperExecution(state=json.loads(json.dumps(execution.dump())))
    position = start_exits(execution, state=json.loads(json.dumps(position.dump())))
    cancelled, stop_order = position.advance(1002.4, book(1002.4, bids=((.24, 4),)))
    assert cancelled.kind == "target_cancelled"
    assert (stop_order.kind, stop_order.limit, stop_order.quantity) == ("stop_submitted", D(".22"), D("1"))
    unfilled, = position.advance(1002.600001, book(1002.600001, bids=((.21, 4),)))
    assert unfilled.match.outcome == "no_fill"
    retry, = position.advance(1004.4, book(1004.4, bids=((.21, 4),)))
    assert (retry.kind, retry.limit) == ("stop_submitted", D(".19"))
    partial, = position.advance(1004.600001, book(1004.600001, bids=((.22, .5),)))
    assert (partial.kind, partial.match.outcome, partial.quantity) == ("stop_ioc", "partial", D(".50"))
    assert position.advance(1010, book(1010, bids=((.90, 30),)), official_yes_payout=1) == ()
    assert position.advance(1014.9, None, official_yes_payout=1) == ()
    assert position.state == "awaiting_settlement"
    settlement, = position.advance(1015, None, official_yes_payout=1)
    assert position.state == "closed"
    assert (settlement.quantity, settlement.proceeds) == (D(".50"), D(".50"))
    assert (position.gross, position.fees, position.settled_proceeds) == (D(".860"), D(".03"), D(".50"))


def test_lost_stop_arrival_quote_marks_result_unverified_without_creating_a_fill():
    position = start_exits(PaperExecution(), side="DOWN", window_end=1050)
    trigger, order = position.advance(1001, book(1001, offers=((.80, 6),)))
    assert trigger.kind == "stop_triggered"
    assert order.limit == D(".18")
    attempt, = position.advance(1001.200001, None)
    assert attempt.match.outcome == "unverified"
    assert position.remaining == 2 and position.unverified
    assert position.advance(1050, None) == ()
    payout, = position.advance(1051, None, official_yes_payout=0)
    assert (payout.kind, payout.proceeds) == ("settled", D("2"))
    assert position.coverage_reason == "book_unavailable"


def test_stop_uses_one_second_decision_cadence_and_never_cancels_pending_target():
    position = start_exits(PaperExecution(), target_latency=1.5, decision_interval=1)
    low = lambda at: book(at, bids=((.24, 9),))
    position.advance(1000, book(1000))
    assert position.advance(1001.1, low(1001.1)) == ()
    assert position.stop_at is None
    working, trigger, cancel = position.advance(1001.5, low(1001.5))
    assert [event.kind for event in (working, trigger, cancel)] == [
        "target_working", "stop_triggered", "target_cancel_requested",
    ]
    cancelled, = position.advance(1001.9, low(1001.9))
    assert cancelled.kind == "target_cancelled"
    assert position.next_action_at == 1002.5
    assert position.advance(1002.49, low(1002.49)) == ()
    submitted, = position.advance(1002.5, low(1002.5))
    assert submitted.kind == "stop_submitted"


def test_active_target_is_cancelled_at_cutoff_and_waits_for_official_payout():
    position = start_exits(PaperExecution(), decision_interval=1)
    position.advance(1008, book(1008))
    # Too late to send a new target inside the entry/exit safety buffer.
    events = position.advance(1009, book(1009))
    assert [event.kind for event in events] == ["target_working"]
    cancel, = position.advance(1010, book(1010, bids=((.99, 100),)), official_yes_payout=1)
    assert cancel.kind == "target_cancel_requested"
    assert position.state == "target_cancelling" and position.remaining == 2
    cancelled, = position.advance(1010.4, None)
    assert cancelled.kind == "target_cancelled"
    assert position.advance(1015, None) == ()
    assert position.state == "awaiting_settlement"
    payout, = position.advance(1016, None, official_yes_payout=1)
    assert payout.proceeds == 2
