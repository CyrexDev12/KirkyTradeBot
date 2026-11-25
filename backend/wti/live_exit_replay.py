"""Attribute Oil D live P&L with official settlement and recorded exit books."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import json
from pathlib import Path
import time

from .commodity import WTI
from .live_exit_replay_books import DEFAULT_DEPTH_SHARE, ExitLeg, benchmark_exit_books
from .live_exit_replay_data import load_live_trades, load_tape_markets
from .live_store import live_path
from .tape import tape_path

ZERO, ONE = Decimal(0), Decimal(1)


def amount(value):
    return Decimal(str(value))


def hold_value(trade, yes_payout):
    if yes_payout is None or trade.entry.fees is None:
        return None, None
    payout = yes_payout if trade.side == "UP" else ONE - yes_payout
    net = trade.entry.filled_quantity * (payout - trade.entry.average_price) - trade.entry.fees
    return payout, net


def stop_at(entry_price, tick, stop_fraction):
    if tick is None or tick <= ZERO:
        return None
    return max(tick, (entry_price * (ONE - stop_fraction) / tick).to_integral_value(rounding=ROUND_DOWN) * tick)


def hold_totals(rows):
    matched = [row for row in rows if row["hold_net"] is not None]
    actual = sum((row["actual_net"] for row in matched), ZERO)
    held = sum((row["hold_net"] for row in matched), ZERO)
    return {
        "eligible": len(rows), "matched": len(matched), "actual_net": actual,
        "hold_net": held, "hold_minus_actual": held - actual,
        "wins_at_settlement": sum(row["hold_net"] > ZERO for row in matched),
        "stopped_but_profitable_at_settlement": sum(row["actual_net"] < ZERO and row["hold_net"] > ZERO
                                                   for row in matched if row["stop_quantity"] > ZERO),
    }


def stop_quote_rows(tape, trades, markets, stop_fraction, depth_share, as_of):
    requested, evidence = [], []
    for trade in trades:
        market = markets.get(trade.slug)
        trigger = stop_at(trade.entry.average_price, market.price_tick if market else None, stop_fraction)
        for order in trade.exits:
            if order.kind != "stop_loss" or order.filled_quantity <= ZERO:
                continue
            data = {
                "trade_id": trade.id, "order_id": order.id, "slug": trade.slug, "side": trade.side,
                "sent_at": order.sending_at, "first_exchange_fill_at": order.first_exchange_fill_at,
                "quantity": order.filled_quantity, "limit": order.limit_price, "actual_price": order.average_price,
                "actual_gross": (order.average_price * order.filled_quantity if order.average_price is not None else None),
                "actual_fee": order.fees, "stop_trigger": trigger,
                "trigger_gross": trigger * order.filled_quantity if trigger is not None else None,
            }
            evidence.append(data)
            if order.sending_at is None:
                data["quote_result"] = "missing_send_time"
            else:
                leg = ExitLeg(trade.slug, trade.window_start, trade.window_end, order.sending_at, trade.side,
                              order.filled_quantity, order.limit_price)
                requested.append((leg, data))
    quotes = benchmark_exit_books(tape, [leg for leg, _ in requested], depth_share=depth_share, as_of=as_of)
    for (_, data), quote in zip(requested, quotes, strict=True):
        data.update(quote_result=quote.reason, book_at=quote.book_at, exchange_at=quote.book_exchange_at,
                    best_bid=quote.best_bid, total_visible_depth=quote.total_visible_depth,
                    visible_depth=quote.visible_depth, executable_quantity=quote.executable_quantity,
                    book_average_price=quote.average_price, book_gross=quote.gross_proceeds)
    return evidence


def stop_totals(rows):
    both = [row for row in rows if row.get("book_gross") is not None and row["actual_gross"] is not None]
    triplets = [row for row in both if row["trigger_gross"] is not None]
    trigger_only = [row for row in rows if row["trigger_gross"] is not None and row["actual_gross"] is not None]
    return {
        "orders": len(rows), "quote_results": Counter(row["quote_result"] for row in rows),
        "trigger_vs_actual_matched": len(trigger_only), "book_vs_actual_matched": len(both),
        "trigger_book_actual_matched": len(triplets),
        "actual_minus_trigger_gross": sum((row["actual_gross"] - row["trigger_gross"] for row in trigger_only), ZERO),
        "actual_minus_book_gross": sum((row["actual_gross"] - row["book_gross"] for row in both), ZERO),
        "book_minus_trigger_gross_on_common_legs": sum((row["book_gross"] - row["trigger_gross"]
                                                       for row in triplets), ZERO),
        "actual_minus_book_gross_on_common_legs": sum((row["actual_gross"] - row["book_gross"]
                                                      for row in triplets), ZERO),
        "book_matched_quantity": sum((row["quantity"] for row in both), ZERO),
    }


def select_trades(journal, since, as_of, cohort):
    if cohort is not None:
        since = min((row["signal_at"] for row in cohort.values()), default=since)
    trades = load_live_trades(journal, since, as_of)
    missing = []
    if cohort is not None:
        actual = {trade.id: trade for trade in trades}
        trades = []
        for key, expected in cohort.items():
            trade = actual.get(key)
            if (trade is None or (trade.slug, trade.side, trade.entry.filled_quantity, trade.entry.average_price)
                    != (expected["slug"], expected["side"], amount(expected["quantity"]), amount(expected["price"]))):
                missing.append(key)
            else:
                trades.append(trade)
    return trades, missing


def official_payout(trade, market, public_payouts, as_of):
    recorded = market.yes_payout if market is not None else None
    public = (public_payouts or {}).get(trade.slug) if trade.window_end <= as_of else None
    if recorded is not None and public is not None and recorded != public:
        return None, "conflicting_official_payouts"
    if recorded is not None:
        return recorded, "recorded_official_payout"
    return public, "verified_public_payout" if public is not None else None


def report(journal, tape, since, as_of, *, stop_fraction, depth_share=DEFAULT_DEPTH_SHARE, cohort=None,
           official_payouts=None):
    trades, missing = select_trades(journal, since, as_of, cohort)
    markets = load_tape_markets(tape, trades, as_of=as_of)
    rows = []
    for trade in sorted(trades, key=lambda row: row.closed_at):
        result, settlement_source = official_payout(trade, markets.get(trade.slug), official_payouts, as_of)
        payout, held = hold_value(trade, result)
        rows.append({
            "trade_id": trade.id, "slug": trade.slug, "side": trade.side, "qty": trade.entry.filled_quantity,
            "entry_price": trade.entry.average_price, "entry_fee": trade.entry.fees, "actual_net": trade.net_pnl,
            "actual_exit_reason": trade.exit_reason, "official_side_payout": payout, "hold_net": held,
            "settlement_source": settlement_source,
            "actual_sell_quantity": sum((order.filled_quantity for order in trade.exits), ZERO),
            "stop_quantity": sum((order.filled_quantity for order in trade.exits if order.kind == "stop_loss"), ZERO),
            "closed_at": trade.closed_at,
        })
    stops = stop_quote_rows(tape, trades, markets, stop_fraction, depth_share, as_of)
    stopped = [row for row in rows if row["stop_quantity"] > ZERO]
    return {
        "trades": len(rows), "expected_trades": len(cohort) if cohort is not None else None,
        "missing_or_mismatched_trade_ids": missing,
        "actual_net": sum((row["actual_net"] for row in rows), ZERO),
        "actual_wins": sum(row["actual_net"] > ZERO for row in rows),
        "actual_losses": sum(row["actual_net"] < ZERO for row in rows),
        "hold_comparison": hold_totals(rows),
        "stopped_trade_hold_comparison": hold_totals(stopped),
        "stop_execution_comparison": stop_totals(stops),
        "stop_fraction": stop_fraction, "displayed_depth_share": depth_share, "details": rows, "stop_details": stops,
    }


def usd(value):
    return f"${value:+.4f}"


def print_report(result, detail=False):
    print(f"Oil D live exit replay | {result['trades']} closed WebSocket trades")
    if result["expected_trades"] is not None:
        print(f"Matched {result['trades']}/{result['expected_trades']} trades from the original export")
    if result["missing_or_mismatched_trade_ids"]:
        print("Missing or changed trade IDs | " + ", ".join(map(str, result["missing_or_mismatched_trade_ids"])))
    print(f"Actual net {usd(result['actual_net'])} | {result['actual_wins']} wins / {result['actual_losses']} losses")
    for label, key in (("All trades", "hold_comparison"), ("Trades with stop fills", "stopped_trade_hold_comparison")):
        row = result[key]
        print(f"{label} | official settlement coverage {row['matched']}/{row['eligible']}")
        if row["matched"]:
            print(f"  Same entries | actual {usd(row['actual_net'])} | hold {usd(row['hold_net'])} | "
                  f"hold minus actual {usd(row['hold_minus_actual'])}")
            if key == "stopped_trade_hold_comparison":
                print(f"  {row['stopped_but_profitable_at_settlement']} actual losers would have been profitable at settlement")
    exits = result["stop_execution_comparison"]
    print(f"Filled stop orders | {exits['orders']} | full fresh-book comparison {exits['book_vs_actual_matched']}")
    if exits["quote_results"]:
        print("  " + ", ".join(f"{key} {value}" for key, value in exits["quote_results"].most_common()))
    if exits["trigger_vs_actual_matched"]:
        print(f"  Actual minus reconstructed stop trigger, gross | "
              f"{usd(exits['actual_minus_trigger_gross'])} on {exits['trigger_vs_actual_matched']} legs")
    if exits["book_vs_actual_matched"]:
        print(f"  Actual minus displayed executable proceeds, gross | "
              f"{usd(exits['actual_minus_book_gross'])} on {exits['book_vs_actual_matched']} legs")
    if exits["trigger_book_actual_matched"]:
        print(f"  Common {exits['trigger_book_actual_matched']} legs | displayed book minus trigger "
              f"{usd(exits['book_minus_trigger_gross_on_common_legs'])} | actual minus displayed book "
              f"{usd(exits['actual_minus_book_gross_on_common_legs'])}")
    print(f"Stop trigger reconstruction uses {result['stop_fraction']:.0%} below entry, rounded to recorded ticks. "
          f"Send-time book replay uses {result['displayed_depth_share']:.0%} of displayed depth at the actual limit.")
    print("Hold uses official payout and actual entry fees. The trigger is a price reference. "
          "Displayed liquidity estimates what the recorded book showed before the exchange processed the order.")
    if detail:
        for row in result["details"]:
            stamp = datetime.fromtimestamp(row["closed_at"], timezone.utc).strftime("%m-%d %H:%M:%SZ")
            held = "unverified" if row["hold_net"] is None else usd(row["hold_net"])
            print(f"{stamp} | {row['slug']} {row['side']} ×{row['qty']} | {row['actual_exit_reason']} | "
                  f"actual {usd(row['actual_net'])} | hold {held}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=live_path(WTI.directory()))
    parser.add_argument("--tape", type=Path, default=tape_path(WTI.directory()))
    parser.add_argument("--hours", type=int, default=168)
    parser.add_argument("--cohort", type=Path, help="exact exported trade IDs and their original entry details")
    parser.add_argument("--official-payouts", type=Path, help="supplemental verified venue payout JSON")
    parser.add_argument("--stop-fraction", type=Decimal, default=amount(WTI.settings().stop_loss or 0.5))
    parser.add_argument("--depth-share", type=Decimal, default=DEFAULT_DEPTH_SHARE)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if (args.hours < 1 or not args.stop_fraction.is_finite() or not ZERO < args.stop_fraction < ONE
            or not args.depth_share.is_finite() or not ZERO < args.depth_share <= ONE):
        parser.error("expected positive hours, a stop fraction between 0 and 1, and a depth share up to 1")
    if not args.journal.is_file() or not args.tape.is_file():
        parser.error("the live order journal and WTI replay tape must both exist")
    try:
        cohort, official_payouts = read_inputs(args.cohort, args.official_payouts)
    except ValueError as exc:
        parser.error(str(exc))
    now = time.time()
    result = report(args.journal, args.tape, now - 3600 * args.hours, now, stop_fraction=args.stop_fraction,
                    depth_share=args.depth_share, cohort=cohort, official_payouts=official_payouts)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print_report(result, args.detail)


def read_inputs(cohort_path, payouts_path):
    cohort = None
    if cohort_path:
        cohort = {int(row["trade_id"]): row for row in json.loads(cohort_path.read_text(encoding="utf-8"))}
    payouts = None
    if payouts_path:
        source = json.loads(payouts_path.read_text(encoding="utf-8"))["payouts"]
        payouts = {slug: amount(value) for slug, value in source.items()}
        if any(not value.is_finite() or not ZERO <= value <= ONE for value in payouts.values()):
            raise ValueError("official payout values must be between 0 and 1")
    return cohort, payouts


if __name__ == "__main__":
    main()
