from dataclasses import replace
from decimal import Decimal

import pytest

from backend.updown.strategy import Book, Settings, Window
from backend.wti.oil_d_shadow_strategy import signal_for_quote, simulate_arrival


D = Decimal
WINDOW = Window("wti", "wti-window", 0, 900, 71.0, "MARKET_STATUS_OPEN", .095)
SETTINGS = Settings()


def book(at, offers=((.40, 20),), *, bids=((.37, 20),), exchange_at=None, sequence=None, requested_at=None):
    return Book(bids, offers, at, at if exchange_at is None else exchange_at, requested_at, sequence)


def signal(quote=None, *, now=120, fair_up=.70, step=.01, window=WINDOW, settings=SETTINGS):
    return signal_for_quote(window, quote or book(now, sequence=10), now=now, fair_up=fair_up, settings=settings,
                            balance=100, open_stake=0, today_pnl=0, minimum_quantity=step)


def arrive(intent, quote, *, now=122, step=.01, tick=.01, funds=12, pnl=0, market_open=True, **caps):
    return simulate_arrival(intent, quote, now=now, minimum_quantity=step, price_tick=tick,
                            buying_power=funds, net_pnl=pnl, market_open=market_open, **caps)


def test_signal_uses_actual_venue_lot_without_original_whole_contract_constraint():
    small_book = book(120, ((.80, 2),), bids=((.79, 20),))
    whole = signal(small_book, fair_up=.98, step=1)
    assert whole.intent is None
    assert "stake too small" in whole.reason

    fractional = signal(small_book, fair_up=.98)
    assert fractional.intent.quantity == D(".50")
    assert fractional.intent.limit == D(".8")
    assert fractional.decision.quantity == .5
    execution = arrive(fractional.intent, book(122, ((.80, 2),), bids=((.79, 20),)))
    assert execution.reason == "filled"
    assert execution.fill.quantity == D(".46")
    assert execution.fill.cost <= fractional.intent.budget


def test_caps_cash_and_each_filled_level_with_original_limit_and_model_fees():
    intent = signal().intent
    execution = arrive(intent, book(122, ((.39, 4), (.40, 40), (.42, 20))))
    assert execution.reason == "filled"
    fill = execution.fill
    assert (fill.side, fill.limit, fill.quantity) == ("UP", D(".40"), D("2.27"))
    assert fill.levels == ((D(".39"), D("1.00")), (D(".40"), D("1.27")))
    assert fill.average_price == (D(".39") + D(".40") * D("1.27")) / D("2.27")
    assert fill.fee == D(".095") * (D(".39") * D(".61") + D(".40") * D(".60") * D("1.27"))
    assert fill.cost < D("1.00")
    assert execution.eligible_depth == 44


def test_higher_paper_caps_still_reserve_the_fee_and_bound_new_risk_by_remaining_loss_room():
    intent = replace(signal().intent, quantity=D("100"), budget=D("90"))
    quote = book(122, ((.40, 400),))
    caps = {"pilot_budget": D("100"), "entry_cap": D("10"), "loss_cap": D("20")}
    assert arrive(intent, quote, funds=100).fill.quantity == D("2.27")
    larger = arrive(intent, quote, funds=100, **caps).fill
    assert larger.quantity == D("23.20")
    assert larger.quantity * D(".43") + D(".02") <= D("10")
    assert (larger.quantity + D(".01")) * D(".43") + D(".02") > D("10")

    higher_fee = arrive(replace(intent, theta=D(".20")), quote, funds=100, **caps).fill
    assert higher_fee.quantity == D("22.17")
    assert higher_fee.quantity * D(".45") + D(".02") <= D("10")
    assert (higher_fee.quantity + D(".01")) * D(".45") + D(".02") > D("10")

    near_guard = arrive(intent, quote, funds=100, pnl=D("-19.25"), **caps).fill
    assert near_guard.quantity == D("1.69")
    assert near_guard.quantity * D(".43") + D(".02") <= D(".75")
    assert arrive(intent, quote, funds=100, pnl=D("-20"), **caps).reason == "loss_cap_reached"
    assert arrive(intent, quote, funds=100, pnl=D("-19.98"), **caps).reason == "budget_below_minimum"
    assert arrive(intent, quote, entry_cap=0, loss_cap=D("20")).reason == "invalid_contract_terms_or_budget"


def test_down_intent_uses_side_specific_asks_without_switching_sides():
    intent = signal(book(120, ((.62, 12),), bids=((.60, 10),)), fair_up=.30).intent
    assert (intent.side, intent.limit) == ("DOWN", D(".40"))
    arrived = book(122, ((.64, 20),), bids=((.61, 4), (.60, 20)))
    execution = arrive(intent, arrived)
    assert execution.fill.side == "DOWN"
    assert execution.fill.levels == ((D(".39"), D("1.00")), (D(".4"), D("1.27")))


@pytest.mark.parametrize(("quote", "now", "market_open", "reason"), [
    (book(121), 121, True, "waiting_for_latency"),
    (book(121.99, requested_at=122), 122, True, "frame_before_ready"),
    (book(122, exchange_at=120, sequence=10), 122, True, "book_not_advanced"),
    (book(122, sequence=9), 122, True, "book_out_of_order"),
    (book(122, exchange_at=106), 122, True, "book_stale"),
    (book(122), 122, False, "market_not_open"),
    (book(128), 128, True, "signal_expired"),
])
def test_no_arrival_without_fresh_post_ready_evidence(quote, now, market_open, reason):
    intent = signal().intent
    execution = arrive(intent, quote, now=now, market_open=market_open)
    assert execution.reason == reason
    assert execution.fill is None


def test_does_not_chase_limit_or_trade_without_market_depth_and_risk_budget():
    intent = signal().intent
    expensive = arrive(intent, book(122, ((.41, 100),)))
    assert (expensive.reason, expensive.best_ask, expensive.eligible_depth) == ("price_above_limit", D(".41"), 0)
    thin = arrive(intent, book(122, ((.40, .03),)))
    assert (thin.reason, thin.eligible_depth, thin.required_depth) == ("insufficient_depth", D(".03"), D(".04"))
    assert arrive(intent, book(122), funds=.02).reason == "budget_below_minimum"
    assert arrive(intent, book(122), pnl=-2).reason == "loss_cap_reached"
    assert arrive(intent, book(122), pnl=-1.50).fill.cost < D(".50")


def test_respects_paper_model_edge_market_and_near_close_cutoff():
    weak = signal(book(120, ((.40, 20),), bids=((.38, 20),)), fair_up=.43)
    assert weak.intent is None
    assert "below" in weak.reason
    assert signal(window=replace(WINDOW, asset="natgas")).reason == "wrong_market"
    assert signal(window=replace(WINDOW, status="MARKET_STATUS_CLOSED")).intent is None
    assert signal(now=809).reason == "signal_expires_before_ready"
    assert signal(now=805).intent.deadline == 808
