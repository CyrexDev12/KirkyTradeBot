"""Compare three exit rules on the same newly recorded Oil D WebSocket shadow fills."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import json
import logging
from pathlib import Path
import sqlite3
import time

import httpx

from ..trading.venue import BASE, ReadOnlyVenueClient
from ..updown.feed import Feed
from ..updown.strategy import wti_products
from .commodity import WTI
from .live_exit_policy_books import exit_timeline, load_exit_books
from .live_exit_policy_strategy import ReplayPosition, replay_policies
from .live_timing import as_decimal, readonly
from .oil_d_shadow_store import OilDShadowStore, oil_d_shadow_path
from .tape import Tape, tape_path


LOG = logging.getLogger(__name__)
VERSION = 1
STAGE = "exit_comparison"
CHECK_SECONDS = 15
SETTLEMENT_INTERVAL = 60
FRAME_FLUSH_GRACE = 4
ZERO = Decimal(0)
POLICY_NAMES = {
    "immediate_stop": "Current stop",
    "confirmed_stop": "3-second confirmed stop",
    "target_then_settlement": "Take profit or settle",
}


def forward_context(window, settings, theta):
    """Freeze exit settings alongside the simulated entry before the account changes."""
    return {
        "version": VERSION, "window_start": window.start, "window_end": window.end,
        "price_tick": window.price_tick, "minimum_quantity": window.minimum_quantity,
        "fee_coefficient": theta, "take_profit_fraction": settings.take_profit_target or .5,
        "stop_loss_fraction": settings.stop_loss or .5,
    }


def selected_entries(path, since, as_of, *, unfinished=False):
    if not path.is_file():
        return []
    with readonly(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='oil_d_shadow'").fetchone() is None:
            return []
        rows = db.execute("""SELECT e.slug,e.signal_at,e.at,e.payload,c.payload
            FROM oil_d_shadow e
            LEFT JOIN oil_d_shadow c ON c.slug=e.slug AND c.signal_at=e.signal_at AND c.stage=? AND c.at<=?
            WHERE e.stage='execution' AND json_extract(e.payload,'$.outcome')='filled'
                AND json_extract(e.payload,'$.forward_exits.version')=? AND e.at BETWEEN ? AND ?
                AND (NOT ? OR (json_extract(e.payload,'$.forward_exits.window_end')<=?
                    AND (c.slug IS NULL OR (json_extract(c.payload,'$.outcome')='pending_settlement' AND c.at<=?))))
            ORDER BY coalesce(c.at,e.at),e.slug,e.signal_at""",
                          (STAGE, as_of, VERSION, since, as_of, unfinished, as_of - FRAME_FLUSH_GRACE,
                           as_of - CHECK_SECONDS))
        return [(slug, signal, filled, json.loads(entry), json.loads(result) if result else None)
                for slug, signal, filled, entry, result in rows]


def official_payout(tape, slug, start, end, as_of):
    with readonly(tape) as db:
        row = db.execute("""SELECT payout FROM markets WHERE slug=? AND start=? AND end=?
                           AND settled_at>=end AND settled_at<=?""",
                         (slug, start, end, as_of)).fetchone()
    amount = as_decimal(row[0]) if row is not None else None
    return amount if amount is not None and 0 <= amount <= 1 else None


def evaluate_entry(tape, slug, entry, now):
    context, fill = entry["forward_exits"], entry["fill"]
    window, frames = load_exit_books(tape, {"slug": slug, **context}, now)
    cost = as_decimal(fill.get("cost"))
    if window is None:
        return {"outcome": "unverified", "reason": "market_not_recorded", "entry_cost": cost}, None
    units = [as_decimal(context.get(key)) for key in ("price_tick", "minimum_quantity", "fee_coefficient")]
    if cost is None or any(value is None for value in units):
        return {"outcome": "unverified", "reason": "entry_cost_or_market_terms_missing", "entry_cost": cost}, None
    payout = official_payout(tape, slug, window.start, window.end, now)
    position = ReplayPosition(
        side=fill["side"], quantity=as_decimal(fill.get("quantity")), entry_price=as_decimal(fill.get("average_price")),
        entry_fee=as_decimal(fill.get("fee")), entry_at=fill["filled_at"], window_end=window.end,
        price_tick=units[0], minimum_quantity=units[1], fee_coefficient=units[2], yes_payout=payout,
    )
    policies = replay_policies(
        position, exit_timeline(window, frames, as_of=now), take_profit_fraction=context["take_profit_fraction"],
        stop_loss_fraction=context["stop_loss_fraction"],
    )
    cases = {}
    for name, policy in policies.items():
        details = asdict(policy)
        if policy.net_pnl is not None:
            details["net_pnl"] = (policy.target_gross + policy.stop_gross + policy.settlement_proceeds
                                  - policy.exit_fees - cost)
        cases[name] = details
    waiting = any(case.reason == "official_settlement_unavailable" for case in policies.values())
    unavailable = sorted({case.reason for case in policies.values() if case.reason is not None})
    result = {
        "outcome": "pending_settlement" if waiting else "unverified" if unavailable else "modeled",
        "reason": ", ".join(unavailable) or None, "entry_cost": cost, "entry_quantity": position.quantity,
        "entry_fee": position.entry_fee, "side": position.side, "official_yes_payout": payout, "policies": cases,
    }
    return result, (slug, window.start) if waiting else None


def advance_exits(directory, store, now):
    """Score completed windows using only captured WS evidence and official payout known by now."""
    tape = tape_path(directory)
    if not tape.is_file():
        return []
    missing = []
    for slug, signal, _, entry, previous in selected_entries(store.path, 0, now, unfinished=True):
        context = entry["forward_exits"]
        start, end = context["window_start"], context["window_end"]
        if previous is not None and official_payout(tape, slug, start, end, now) is None:
            missing.append((slug, start))
            continue
        outcome, awaiting = evaluate_entry(tape, slug, entry, now)
        store.record(slug, signal, STAGE, now, outcome)
        if awaiting is not None:
            missing.append(awaiting)
    return missing


async def run_forward_shadow_exits(directory, stop):
    """Collect only missing official payouts on a paced read-only public feed."""
    store = None
    next_settlement = float("-inf")
    requested = {}
    product, = wti_products(("15m",))
    async with httpx.AsyncClient(base_url=BASE, timeout=3) as client:
        feed = None
        while not stop.is_set():
            try:
                if store is None:
                    store = await asyncio.to_thread(OilDShadowStore, oil_d_shadow_path(directory))
                now = time.time()
                waiting = await asyncio.to_thread(advance_exits, directory, store, now)
                monotonic = time.monotonic()
                if waiting and monotonic >= next_settlement:
                    slug, start = min(waiting, key=lambda item: requested.get(item[0], float("-inf")))
                    requested = {key: requested[key] for key, _ in waiting if key in requested}
                    next_settlement = monotonic + SETTLEMENT_INTERVAL
                    requested[slug] = monotonic
                    if feed is None:
                        feed = Feed(ReadOnlyVenueClient(client, min_interval=.2, max_retries=0), client)
                    result = await asyncio.wait_for(feed.settlement(product, start), timeout=4)
                    if result is not None and result.slug == slug:
                        tape = await asyncio.to_thread(Tape, tape_path(directory))
                        await asyncio.to_thread(tape.record_settlement, result, time.time())
            except (OSError, sqlite3.Error, TimeoutError, httpx.HTTPError, KeyError, TypeError,
                    ValueError, ArithmeticError) as exc:
                LOG.warning("Oil D shadow exit evidence unavailable (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=CHECK_SECONDS)
            except TimeoutError:
                pass


def report(path: Path, since: float, as_of: float, *, detail=False):
    rows = selected_entries(path, since, as_of)
    statuses, reasons = Counter(), Counter()
    policies = {name: {"modeled_trades": 0, "pending_settlement": 0, "unverified": 0,
                       "common_net": ZERO, "common_wins": 0, "common_losses": 0,
                       "common_target_trades": 0, "common_stop_trades": 0, "common_settled_trades": 0}
                for name in POLICY_NAMES}
    common, common_entry_cost, details = 0, ZERO, []
    for slug, signal, at, entry, result in rows:
        pending_status = "window_open" if entry["forward_exits"]["window_end"] > as_of else "awaiting_comparison"
        statuses[result["outcome"] if result else pending_status] += 1
        if result and result.get("reason"):
            reasons[result["reason"]] += 1
        cases = result.get("policies", {}) if result else {}
        values = {name: as_decimal(cases.get(name, {}).get("net_pnl")) for name in policies}
        comparable = all(value is not None for value in values.values())
        if comparable:
            common += 1
            common_entry_cost += as_decimal(result["entry_cost"])
        for name, value in values.items():
            policy = policies[name]
            policy["modeled_trades"] += value is not None
            if result is not None and value is None:
                reason = cases.get(name, {}).get("reason")
                policy["pending_settlement" if reason == "official_settlement_unavailable" else "unverified"] += 1
            if comparable:
                policy["common_net"] += value
                policy["common_wins"] += value > 0
                policy["common_losses"] += value < 0
                for field in ("target", "stop", "settled"):
                    policy[f"common_{field}_trades"] += (as_decimal(cases[name].get(f"{field}_quantity")) or ZERO) > 0
        if detail:
            details.append({"slug": slug, "signal_at": signal, "filled_at": at, "comparison": result})
    return {"forward_shadow_entries": len(rows), "common_trades": common, "common_entry_cost": common_entry_cost,
            "status": statuses, "unverified_reasons": reasons, "policies": policies, "details": details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--journal", type=Path, default=oil_d_shadow_path(WTI.directory()))
    parser.add_argument("--json", action="store_true", help="include per-entry policy results")
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")
    now = time.time()
    result = report(args.journal, now - args.hours * 3600, now, detail=args.json)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
        return
    print(f"Oil D forward shadow exits | {args.hours}h | {result['forward_shadow_entries']} entries | "
          f"{result['common_trades']} comparable under all three policies")
    if not result["forward_shadow_entries"]:
        print("Waiting for new Oil D WebSocket shadow fills.")
        return
    if result["status"]:
        print("Coverage | " + ", ".join(f"{name.replace('_', ' ')} {count}" for name, count in result["status"].items()))
    for reason, count in result["unverified_reasons"].items():
        print(f"  {reason.replace('_', ' ')} {count}")
    base = result["policies"]["immediate_stop"]["common_net"]
    for name, policy in result["policies"].items():
        line = (f"{POLICY_NAMES[name]} | {policy['modeled_trades']} modeled | "
                f"{policy['pending_settlement']} waiting for payout | {policy['unverified']} unverified")
        if result["common_trades"]:
            net = policy["common_net"]
            line += f" | common net ${net:+.2f} | ${net-base:+.2f} vs current"
        print(line)
    print("All three share the same shadow entries, entry sizes, and costs. Exits use captured WebSocket bids "
          "with modeled fees. Residual positions use official settlement.")


if __name__ == "__main__":
    main()
