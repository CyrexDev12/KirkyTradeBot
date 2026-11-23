"""Operator CLI: python -m backend.trading.cli <command>

  status                    account, latest plan and readiness
  mock-run [--out PATH]     deterministic worked mock run (writes markdown)
  uncertainty [--mc-seasons 2025,2026]
                            build documented model-uncertainty profiles from walk-forward calibration
  label-history             replay recorded books into market-path examples (same engine and config)
  validate                  chronological validation artifact for the current strategy version
  plan --season S --week W  build (or return) the weekly plan; does not activate
  activate --season S --week W
                            revalidate and activate the plan's paper entries
  capabilities              implemented / collecting / unsupported report
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from .common import D, dumps, utc
from .config import StrategyConfig
from .inputs import UNCERTAINTY_MODEL_VERSIONS, UNCERTAINTY_PATH
from .store import Store
from .worker import DB_PATH


def _now():
    return datetime.now(timezone.utc)


def cmd_status(store, cfg, args):
    engine = store.load_engine(cfg)
    plan = store.latest_plan(cfg.strategy_id)
    print(dumps({"account": engine.account(_now()), "latest_plan": plan["summary"] if plan else None,
                 "path_examples": len(store.path_examples()),
                 "validation": (store.latest_validation(cfg.version) or {}).get("passed")}))


def cmd_mock(store, cfg, args):
    from .mock_run import markdown, run
    result = run(args.db)
    text = markdown(result)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    print(text)
    if not result["reconciled"]:
        raise SystemExit("Mock run did not reconcile")


def _merge_bins(bins: list[dict]) -> list[dict]:
    """Pool per-season calibration bins with the same decile index (count-weighted)."""
    pooled: dict = {}
    for b in bins:
        key = b.get("bin", round(float(b["predicted"]), 1))
        n = int(b.get("games") or 0)
        if not n:
            continue
        acc = pooled.setdefault(key, [0, 0.0, 0.0])
        acc[0] += n
        acc[1] += float(b["predicted"]) * n
        acc[2] += float(b["observed"]) * n
    return [{"bin": k, "games": n, "predicted": p / n, "observed": o / n} for k, (n, p, o) in sorted(pooled.items())]


def cmd_uncertainty(store, cfg, args):
    from ..mc_validation import metrics as mc_metrics
    from ..model import get_metrics
    from .valuation import half_width_from_calibration
    profiles = []
    try:
        gb = half_width_from_calibration(get_metrics().get("calibration"), "GB", "moneyline",
                                         "backend.model walk-forward calibration (binary, ties excluded)")
        if gb:
            profiles.append(gb)
    except RuntimeError as exc:
        print(f"GB metrics unavailable: {exc}")
    seasons = [int(s) for s in args.mc_seasons.split(",")] if args.mc_seasons else []
    ml_bins, total_bins = [], []
    for season in seasons:
        report = mc_metrics(season)
        m = report.get("metrics") or {}
        ml_bins += (m.get("binary_no_tie") or {}).get("calibration") or []
        for line in (m.get("total_lines") or {}).values():
            total_bins += (line or {}).get("calibration") or []
    ml_bins, total_bins = _merge_bins(ml_bins), _merge_bins(total_bins)
    if ml_bins:
        p = half_width_from_calibration(ml_bins, "MC", "moneyline",
                                        f"mc_validation reconstructions, seasons {seasons} (no-tie binary bins)")
        profiles += [p] if p else []
    if total_bins:
        p = half_width_from_calibration(total_bins, "MC", "total",
                                        f"mc_validation reconstructions, seasons {seasons} (37.5/44.5/51.5 lines)")
        profiles += [p] if p else []
    UNCERTAINTY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": _now().isoformat(), "model_versions": UNCERTAINTY_MODEL_VERSIONS, "profiles": [
        {"provider": p.provider, "market_type": p.market_type, "half_width": str(p.half_width), "method": p.method,
         "evaluated_games": p.evaluated_games, "source": p.source} for p in profiles],
        "note": "GB total-line probabilities have no calibration bins in the existing metrics; GB totals remain "
                "unsupported until such an evaluation exists."}
    temp = None
    try:
        with NamedTemporaryFile(mode="w", dir=UNCERTAINTY_PATH.parent, delete=False, encoding="utf-8") as file:
            temp = Path(file.name)
            json.dump(payload, file, indent=2)
        temp.replace(UNCERTAINTY_PATH)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    print(json.dumps(payload, indent=2))


def cmd_label(store, cfg, args):
    """Hypothetical decision points every 6 hours over each recorded contract, every grid combination."""
    from contextlib import closing
    from .book import selected_view
    from .contracts import ContractSpec, settlement_payout
    from .inputs import LiveInputs
    from .paper_engine import TradePlan, deadlines
    from .path_model import label_path
    rows = {g["game_id"]: g for g in LiveInputs(None, store).all_games()}
    with closing(store.connect()) as db:
        contracts = [json.loads(r["payload"]) for r in db.execute("SELECT payload FROM market_contracts")]
    examples = []
    for c in contracts:
        game = rows.get(c["game_id"])
        if not game or game.get("home_score") is None or not c.get("tick_size") or not c.get("quantity_step"):
            continue
        snaps = store.snapshots(c["market_slug"], source="rest_book")
        if not snaps:
            continue
        kickoff = utc(c["kickoff"])
        exp, exit_dl = deadlines(kickoff, cfg.hold_policy, cfg)
        t = snaps[0].received_at
        while t < exp:
            at = [s for s in snaps if s.received_at <= t]
            if at:
                view = selected_view(at[-1], c["selected_side"])
                tick = D(c["tick_size"])
                if view.best_ask is not None:
                    for k in cfg.grid_entry_ticks_below_ask:
                        for tt in cfg.grid_target_multiples:
                            for ss in cfg.grid_stop_fractions:
                                from .candidates import floor_tick
                                entry = view.best_ask - tick * k
                                target, stop = floor_tick(entry * D(tt), tick), floor_tick(entry * D(ss), tick)
                                if not D(0) < stop < entry < target < D(1):
                                    continue
                                plan = TradePlan(
                                    trade_id=f"label:{c['market_slug']}:{c['selected_outcome']}:{t.isoformat()}:{k}:{tt}:{ss}",
                                    revision=1, strategy_id=cfg.strategy_id, config_version=cfg.version,
                                    season=game["season"], week=game["week"], game_id=game["game_id"],
                                    market_slug=c["market_slug"], market_type=c["market_type"],
                                    selected_outcome=c["selected_outcome"], selected_side=c["selected_side"],
                                    total_line=D(c["total_line"]) if c.get("total_line") else None,
                                    settlement_rule_version=c["settlement_rule_version"], hold_policy=cfg.hold_policy,
                                    kickoff=kickoff, decision_at=t, entry_active_after=t, entry_expires_at=exp,
                                    exit_deadline=exit_dl, entry_limit=entry, target_limit=target, stop_trigger=stop,
                                    quantity=D(10), tick_size=tick, quantity_step=D(c["quantity_step"]),
                                    min_quantity=D(c.get("min_quantity") or c["quantity_step"]),
                                    estimated_entry_cost=D(10) * entry, planned_loss=D(10) * (entry - stop),
                                    full_funded_loss=D(10) * entry)
                                spec = ContractSpec(**{**c, "kickoff": kickoff,
                                                       "total_line": D(c["total_line"]) if c.get("total_line") else None,
                                                       "tie_payout": D(c["tie_payout"]) if c.get("tie_payout") else None,
                                                       "tick_size": tick, "quantity_step": D(c["quantity_step"]),
                                                       "min_quantity": D(c["min_quantity"]) if c.get("min_quantity") else None})
                                payout = settlement_payout(spec, int(game["home_score"]), int(game["away_score"]))
                                examples.append(label_path(plan, [s for s in snaps if s.received_at >= t], cfg, payout))
            t += timedelta(hours=6)
    store.add_path_examples(examples, _now())
    print(f"Added {len(examples)} replay-labeled examples from {len(contracts)} recorded contracts.")


def cmd_validate(store, cfg, args):
    from .path_model import validate
    artifact = validate(store.path_examples(), cfg)
    store.save_validation(cfg.version, artifact["passed"], artifact, _now())
    print(dumps({"passed": artifact["passed"], "reasons": artifact["reasons"], "buckets": len(artifact["buckets"])}))


def _planner(store, cfg):
    from .inputs import LiveInputs
    from .planner import WeeklyPlanner
    from .venue import ReadOnlyVenueClient
    venue = ReadOnlyVenueClient(min_interval=float(cfg.min_request_interval_seconds))
    return WeeklyPlanner(store, cfg, LiveInputs(venue, store)), venue


def cmd_plan(store, cfg, args):
    planner, venue = _planner(store, cfg)
    async def go():
        try:
            return await planner.run(args.season, args.week, _now())
        finally:
            await venue.close()
    plan = asyncio.run(go())
    print(dumps(plan["summary"]))


def cmd_activate(store, cfg, args):
    from .planner import plan_key
    planner, venue = _planner(store, cfg)
    async def go():
        try:
            return await planner.activate(plan_key(cfg, args.season, args.week), _now())
        finally:
            await venue.close()
    print(dumps(asyncio.run(go())))


CAPABILITIES = {
    "implemented_and_tested": [
        "event-sourced $100 paper ledger with restart rebuild, dedupe, reservations, decimal money",
        "conservative observable-book fills: latency, participation cap, once-only liquidity, partial fills",
        "target/stop/time exits sharing inventory, stop gaps at executable levels, halts and outages without fills",
        "taker fee profile (theta 0.095, effective 2026-11-14) with per-fill cent rounding",
        "moneyline and half-point total contract mapping with explicit YES/NO orientation and settlement rules",
        "separate GB and MC payout adapters; GB tie adapter with an independent tie base-rate range",
        "hard gates with rejection codes, frozen grid, absolute-normalized rating, bounded sizing and drawdown rules",
        "replay labeling through the same engine, censoring-aware analogue estimator, chronological validation",
        "weekly planner with one authoritative key, revalidation before activation, durable jobs and Tuesday fallback",
        "Discord commands, channel routing, durable outbox with reconciliation and in-place card edits",
        "PAPER_ONLY read-only venue client that refuses non-GET or non-allow-listed routes",
    ],
    "collecting_data": [
        "timestamped book snapshots for active and candidate contracts (REST, 5s/60s)",
        "market-path examples accumulate only from recorded books; none exist at first deployment",
    ],
    "not_verified_against_live_endpoints": [
        "Polymarket US route payload fields (tick size, quantity step, market state names, settlement schema)",
        "fee rounding/aggregation rules beyond the existing dashboard convention",
    ],
    "unsupported": [
        "passive queue fill mode (needs trade/depth data)", "authenticated market WebSocket streaming",
        "GB total-line uncertainty (no calibration bins in current metrics)", "integer total lines (push)",
        "player props, period and team totals, postseason MC cases", "fractional Kelly sizing",
        "real order execution (by design)",
    ],
}


def cmd_capabilities(store, cfg, args):
    print(json.dumps(CAPABILITIES, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m backend.trading.cli")
    parser.add_argument("--db", default=None, help="paper SQLite path (default NFL_PAPER_DB or data/trading)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    m = sub.add_parser("mock-run")
    m.add_argument("--out")
    u = sub.add_parser("uncertainty")
    u.add_argument("--mc-seasons", default=None)
    sub.add_parser("label-history")
    sub.add_parser("validate")
    sub.add_parser("capabilities")
    for name in ("plan", "activate"):
        p = sub.add_parser(name)
        p.add_argument("--season", type=int, required=True)
        p.add_argument("--week", type=int, required=True)
    args = parser.parse_args(argv)
    cfg = StrategyConfig.from_env()
    store = Store(args.db or DB_PATH) if args.cmd != "mock-run" else None
    {"status": cmd_status, "mock-run": cmd_mock, "uncertainty": cmd_uncertainty, "label-history": cmd_label,
     "validate": cmd_validate, "plan": cmd_plan, "activate": cmd_activate,
     "capabilities": cmd_capabilities}[args.cmd](store, cfg, args)


if __name__ == "__main__":
    main()
