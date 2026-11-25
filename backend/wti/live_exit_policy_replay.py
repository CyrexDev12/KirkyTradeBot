"""Compare Oil D exit rules on the same confirmed live entries and recorded WebSocket books."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import json
from math import isfinite
from pathlib import Path
import time

from .commodity import WTI
from .live_exit_policy_books import exit_timeline, load_exit_books
from .live_exit_policy_strategy import ReplayPosition, replay_policies
from .live_exit_replay import ONE, ZERO, amount, hold_value, official_payout, read_inputs, select_trades, usd
from .live_exit_replay_books import DEFAULT_DEPTH_SHARE
from .live_exit_replay_data import load_tape_markets
from .live_store import live_path
from .tape import tape_path

POLICIES = ("immediate_stop", "confirmed_stop", "target_then_settlement")


def confirmed_entry_at(entry):
    if sum((fill.quantity for fill in entry.fills), ZERO) != entry.filled_quantity or not entry.fills:
        return None
    return max(max(fill.at, fill.exchange_at or fill.at, fill.observed_at or fill.at) for fill in entry.fills)


def prepare_position(trade, window, yes_payout):
    entry = trade.entry
    if entry.fees is None:
        return None, "missing_actual_entry_fee"
    entry_at = confirmed_entry_at(entry)
    if entry_at is None or not trade.window_start <= entry_at < trade.window_end:
        return None, "missing_or_invalid_entry_fill_timing"
    if window is None:
        return None, "missing_recorded_market"
    if window.price_tick is None or window.minimum_quantity is None:
        return None, "missing_market_increments"
    tick, step, fee = map(amount, (window.price_tick, window.minimum_quantity, window.theta))
    if not all(value.is_finite() for value in (tick, step, fee)) or not (ZERO < tick < ONE and step > ZERO and fee >= ZERO):
        return None, "invalid_market_terms"
    targets = sorted((order for order in trade.exits if order.kind == "target" and order.sending_at is not None),
                     key=lambda order: order.sending_at)
    if targets and not entry_at <= targets[0].sending_at < trade.window_end:
        return None, "invalid_target_handoff"
    target = targets[0] if targets else None
    position = ReplayPosition(
        side=trade.side, quantity=entry.filled_quantity, entry_price=entry.average_price, entry_fee=entry.fees,
        entry_at=entry_at, window_end=trade.window_end, price_tick=tick, minimum_quantity=step, fee_coefficient=fee,
        yes_payout=yes_payout, target_at=target.sending_at if target else None,
        target_limit=target.limit_price if target else None,
    )
    return position, None


def scenario_total(rows, policy):
    cases = [row["scenarios"][policy] for row in rows]
    net_changes = [row["scenarios"][policy]["net_pnl"] - row["scenarios"]["immediate_stop"]["net_pnl"]
                   for row in rows]
    return {
        "net": sum((case["net_pnl"] for case in cases), ZERO),
        "wins": sum(case["net_pnl"] > ZERO for case in cases),
        "losses": sum(case["net_pnl"] < ZERO for case in cases),
        "target_trades": sum(case["target_quantity"] > ZERO for case in cases),
        "stop_trades": sum(case["stop_quantity"] > ZERO for case in cases),
        "settlement_trades": sum(case["settled_quantity"] > ZERO for case in cases),
        "improved_vs_current": sum(delta > ZERO for delta in net_changes),
        "worse_vs_current": sum(delta < ZERO for delta in net_changes),
    }


def report(journal, tape, since, as_of, *, stop_fraction=Decimal(".5"), take_profit_fraction=Decimal(".5"),
           confirm_seconds=3.0, latency=0.25, depth_share=DEFAULT_DEPTH_SHARE, cohort=None, official_payouts=None):
    trades, missing = select_trades(journal, since, as_of, cohort)
    markets = load_tape_markets(tape, trades, as_of=as_of)
    rows = []
    for trade in sorted(trades, key=lambda row: row.closed_at):
        yes, source = official_payout(trade, markets.get(trade.slug), official_payouts, as_of)
        _, pure_hold = hold_value(trade, yes)
        if as_of >= trade.window_end:
            window, frames = load_exit_books(tape, trade, as_of)
            position, reason = prepare_position(trade, window, yes)
        else:
            position, reason = None, "window_still_open"
        scenarios = {}
        if position is not None:
            timeline = tuple(exit_timeline(window, frames, as_of=as_of))
            scenarios = {name: asdict(case) for name, case in replay_policies(
                position, timeline, take_profit_fraction=take_profit_fraction, stop_loss_fraction=stop_fraction,
                confirm_seconds=confirm_seconds, latency=latency, depth_share=depth_share,
            ).items()}
            unverified = sorted({case["reason"] or "unverified_exit_path" for case in scenarios.values()
                                 if case["net_pnl"] is None})
            reason = ", ".join(unverified) or None
        rows.append({
            "trade_id": trade.id, "slug": trade.slug, "side": trade.side, "quantity": trade.entry.filled_quantity,
            "actual_net": trade.net_pnl, "actual_exit_reason": trade.exit_reason,
            "pure_hold_net": pure_hold, "settlement_source": source, "unverified_reason": reason, "scenarios": scenarios,
        })
    common = [row for row in rows if row["unverified_reason"] is None]
    return {
        "trades": len(rows), "expected_trades": len(cohort) if cohort is not None else None,
        "missing_or_mismatched_trade_ids": missing, "common_trades": len(common),
        "actual_net": sum((row["actual_net"] for row in rows), ZERO),
        "actual_net_common": sum((row["actual_net"] for row in common), ZERO),
        "pure_hold_net": sum((row["pure_hold_net"] for row in rows if row["pure_hold_net"] is not None), ZERO),
        "pure_hold_trades": sum(row["pure_hold_net"] is not None for row in rows),
        "unverified_reasons": Counter(row["unverified_reason"] for row in rows if row["unverified_reason"]),
        "scenario_totals": {policy: scenario_total(common, policy) for policy in POLICIES},
        "confirm_seconds": confirm_seconds, "latency": latency, "depth_share": depth_share, "details": rows,
    }


def print_report(result, detail=False):
    print(f"Oil D exit policy replay | {result['trades']} closed WebSocket trades")
    if result["expected_trades"] is not None:
        print(f"Matched {result['trades']}/{result['expected_trades']} original trades")
    if result["missing_or_mismatched_trade_ids"]:
        print("Missing or changed trade IDs | " + ", ".join(map(str, result["missing_or_mismatched_trade_ids"])))
    print(f"Actual net | {usd(result['actual_net'])}")
    print(f"Pure hold with no exits | {usd(result['pure_hold_net'])} on {result['pure_hold_trades']} official payouts")
    print(f"Comparable under all three replay policies | {result['common_trades']}/{result['trades']}")
    if result["unverified_reasons"]:
        print("Unverified | " + ", ".join(f"{reason} {n}" for reason, n in result["unverified_reasons"].most_common()))
    labels = {
        "immediate_stop": "Current stop",
        "confirmed_stop": f"{result['confirm_seconds']:g}s confirmed stop",
        "target_then_settlement": "Take profit or settle",
    }
    if result["common_trades"]:
        baseline = result["scenario_totals"]["immediate_stop"]["net"]
        print(f"Actual on comparable trades | {usd(result['actual_net_common'])}")
        for name, data in result["scenario_totals"].items():
            print(f"{labels[name]} | net {usd(data['net'])} | vs current replay {usd(data['net'] - baseline)} | "
                  f"{data['wins']}W/{data['losses']}L | trades with target {data['target_trades']}, "
                  f"stop {data['stop_trades']}, settlement {data['settlement_trades']}")
            if name != "immediate_stop":
                print(f"  Per-trade differences vs current | {data['improved_vs_current']} better, "
                      f"{data['worse_vs_current']} worse")
        print(f"Current-stop replay minus actual | {usd(baseline - result['actual_net_common'])}")
    print(f"All policies keep the same actual entry size and take-profit rule. "
          f"Replay uses {result['depth_share']:.0%} displayed depth and a {result['latency']:.3f}s modeled latency.")
    print("Take-profit fills require executable bids at the target. Passive queue positions are unavailable. "
          "Exit fees are modeled. A trade can have more than one exit type.")
    if detail:
        for row in result["details"]:
            evidence = row["unverified_reason"]
            values = (" | " + evidence if evidence else " | " + " | ".join(
                f"{labels[name]} {usd(case['net_pnl'])}" for name, case in row["scenarios"].items()))
            print(f"{row['slug']} {row['side']} ×{row['quantity']} | actual {usd(row['actual_net'])}" + values)


def main():
    settings = WTI.settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=live_path(WTI.directory()))
    parser.add_argument("--tape", type=Path, default=tape_path(WTI.directory()))
    parser.add_argument("--cohort", type=Path)
    parser.add_argument("--official-payouts", type=Path)
    parser.add_argument("--hours", type=int, default=168)
    parser.add_argument("--stop-fraction", type=Decimal, default=amount(settings.stop_loss or .5))
    parser.add_argument("--take-profit-fraction", type=Decimal, default=amount(settings.take_profit_target or .5))
    parser.add_argument("--confirm-seconds", type=float, default=3)
    parser.add_argument("--latency", type=float, default=.25)
    parser.add_argument("--depth-share", type=Decimal, default=DEFAULT_DEPTH_SHARE)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    fractions = (args.stop_fraction, args.take_profit_fraction, args.depth_share)
    if (args.hours < 1 or not all(value.is_finite() and ZERO < value <= ONE for value in fractions)
            or ONE in fractions[:2] or not isfinite(args.confirm_seconds) or args.confirm_seconds <= 0
            or not isfinite(args.latency) or args.latency < 0):
        parser.error("check fractions, confirmation window, latency, and report hours")
    if not args.journal.is_file() or not args.tape.is_file():
        parser.error("the live journal and WTI replay tape must both exist")
    try:
        cohort, payouts = read_inputs(args.cohort, args.official_payouts)
    except ValueError as exc:
        parser.error(str(exc))
    now = time.time()
    result = report(args.journal, args.tape, now - 3600 * args.hours, now, stop_fraction=args.stop_fraction,
                    take_profit_fraction=args.take_profit_fraction, confirm_seconds=args.confirm_seconds,
                    latency=args.latency, depth_share=args.depth_share, cohort=cohort, official_payouts=payouts)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print_report(result, args.detail)


if __name__ == "__main__":
    main()
