from dataclasses import replace
from decimal import Decimal

import pytest

from backend.updown.strategy import Book
from backend.wti.live_exit_policy_strategy import ReplayPosition, replay_policies


D = Decimal
POSITION = ReplayPosition("UP", D("1"), D(".40"), D(".01"), 100., 125., D(".01"), D(".01"), D(".095"), D("1"))


def frame(at, bid, depth=4, *, seq=None, exchange_at=None):
    book = Book(((bid, depth),), ((.95, depth),), at, at if exchange_at is None else exchange_at, sequence=seq)
    return at, book, False


def test_confirmation_filters_dip_and_keeps_original_target_anchor_and_limit():
    position = replace(POSITION, target_at=102., target_limit=D(".69"))
    results = replay_policies(position, [frame(100., .19), frame(101., .75), frame(102.1, .73)])
    immediate, confirmed, target_only = (results[key] for key in ("immediate_stop", "confirmed_stop", "target_then_settlement"))
    assert immediate.stop_trigger_at == 100.
    assert immediate.fills[0].at == 100.25
    assert immediate.fills[0].average_price == D(".19")
    assert immediate.fills[0].limit == D(".17")
    assert (immediate.stop_quantity, immediate.target_quantity, immediate.net_pnl) == (1, 0, D("-.23"))
    assert confirmed.stop_trigger_at is None
    assert confirmed.fills[0].at == 102.
    assert confirmed.fills[0].average_price == D(".69")
    assert confirmed.target_quantity == target_only.target_quantity == 1
    assert confirmed.net_pnl == target_only.net_pnl == D(".26")
    assert {case.target_price for case in results.values()} == {D(".69")}


def test_sustained_stop_requires_a_fresh_distinct_frame_at_three_seconds():
    results = replay_policies(POSITION, [frame(100., .18), frame(101.5, .19), frame(103., .17)])
    confirmed = results["confirmed_stop"]
    assert confirmed.stop_trigger_at == 103.
    assert confirmed.fills[0].at == 103.25
    assert confirmed.fills[0].limit == D(".15")
    assert confirmed.fills[0].average_price == D(".17")
    assert results["target_then_settlement"].settled_quantity == 1


@pytest.mark.parametrize("interrupt", ["gap", "halt", "staleness", "recovery", "duplicate"])
def test_stop_confirmation_does_not_bridge_unsupported_duration(interrupt):
    first = frame(100., .18)
    if interrupt in ("gap", "halt"):
        quotes = [first, (101., None, interrupt == "halt"), frame(103., .17)]
    elif interrupt == "staleness":
        quotes = [first, frame(103.25, .17)]
    elif interrupt == "recovery":
        quotes = [first, frame(101., .50), frame(103.25, .17)]
    else:
        quotes = [first, (103., first[1], False)]

    replay = replay_policies(POSITION, quotes)["confirmed_stop"]
    assert replay.status == "modeled"
    assert replay.stop_trigger_at is None
    assert replay.stop_quantity == 0
    assert replay.settled_quantity == 1


def test_retries_cannot_reuse_spent_depth_and_use_only_justified_partial_fills():
    replay = replay_policies(POSITION, [frame(100., .19, 2), frame(104., .19, 4)])["immediate_stop"]
    assert [(fill.at, fill.quantity) for fill in replay.fills] == [(100.25, D(".50")), (104.25, D(".50"))]
    assert replay.settled_quantity == 0


def test_stop_uses_last_legal_snapshot_as_of_arrival_and_cannot_fill_after_cutoff():
    early, late = frame(100., .19), frame(100.5, .60)
    liquidity = replay_policies(POSITION, [early, late])["immediate_stop"]
    assert liquidity.fills[0].average_price == D(".19")
    at_cutoff = replace(POSITION, window_end=106.25)
    result = replay_policies(at_cutoff, [early, late], latency=1.25)["immediate_stop"]
    assert result.stop_trigger_at == 100.
    assert not result.fills and result.settled_quantity == 1


def test_stop_ioc_preserves_order_floor_across_gap_then_reprices_next_attempt():
    replay = replay_policies(POSITION, [frame(100., .19), (100.1, None, False),
                                        frame(100.2, .10), frame(102., .09)])["immediate_stop"]
    assert replay.stop_trigger_at == 100.
    assert [(fill.at, fill.limit, fill.average_price) for fill in replay.fills] == [(102.25, D(".07"), D(".09"))]


def test_unavailable_books_or_official_settlement_do_not_create_hypothetical_net():
    empty = replay_policies(POSITION, [(101., None, False)])
    assert {case.reason for case in empty.values()} == {"no_usable_bid_frames"}
    assert all(case.net_pnl is None for case in empty.values())
    unknown = replay_policies(replace(POSITION, yes_payout=None), [frame(101., .50)])
    assert {case.reason for case in unknown.values()} == {"official_settlement_unavailable"}
    assert all(case.net_pnl is None for case in unknown.values())


def test_down_settlement_uses_complement_payout_on_remaining_real_size():
    position = replace(POSITION, side="DOWN", yes_payout=D("0"), quantity=D("1.75"))
    quote = Book(((.20, 40),), ((.50, 40),), 101., 101.)
    replay = replay_policies(position, [(101., quote, False)])["target_then_settlement"]
    assert replay.settlement_proceeds == D("1.75")
    assert replay.net_pnl == D("1.04")


def test_long_missing_tail_disqualifies_open_quantity_but_not_an_already_closed_scenario():
    position = replace(POSITION, window_end=160.)
    results = replay_policies(position, [frame(100., .19), (104., None, False)])
    assert results["immediate_stop"].status == "modeled"
    assert results["immediate_stop"].stop_quantity == 1
    for key in ("confirmed_stop", "target_then_settlement"):
        assert results[key].reason == "extended_unobserved_quotes"
        assert results[key].net_pnl is None
        assert results[key].longest_unobserved_seconds == 52


def test_late_first_usable_bid_cannot_verify_an_earlier_gap():
    position = replace(POSITION, window_end=145.)
    results = replay_policies(position, [frame(123., .75)])
    assert all(case.reason == "extended_unobserved_quotes" for case in results.values())
    assert all(case.longest_unobserved_seconds == 23 for case in results.values())


def test_authoritative_halt_does_not_count_toward_unknown_quote_coverage():
    position = replace(POSITION, window_end=160.)
    results = replay_policies(position, [frame(100., .40), (102., None, True), frame(151., .40)])
    assert all(case.status == "modeled" for case in results.values())
    assert all(case.longest_unobserved_seconds == 1 for case in results.values())
