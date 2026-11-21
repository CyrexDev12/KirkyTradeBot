"""Durable weekly scheduling: slate completion, reports, planning, Tuesday fallback.

The end of an NFL slate is detected from event state (every game in the week
final or canceled), not from a fixed Monday clock time, so Monday
doubleheaders, holiday games, rescheduling and weeks without a Monday game are
handled the same way. Job keys make every step idempotent across restarts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .common import D, utc
from .config import StrategyConfig
from .contracts import ContractSpec, settlement_payout
from .planner import WeeklyPlanner, plan_key
from .reports import lifecycle_alerts, weekly_report
from .store import Store

CANCELED_STATES = {"cancelled", "canceled"}


def is_final(game: dict) -> bool:
    return game.get("home_score") is not None and game.get("away_score") is not None


def is_canceled(game: dict) -> bool:
    return str(game.get("source_status") or "").lower() in CANCELED_STATES


def week_sequence(rows: list[dict]) -> list[tuple[int, int]]:
    return sorted({(g["season"], g["week"]) for g in rows})


def previous_and_next(rows: list[dict], now: datetime) -> tuple[tuple | None, tuple | None]:
    """Latest (season, week) with a kicked-off game, and the following week."""
    seq = week_sequence(rows)
    started = [k for k in seq if any(utc(g["kickoff"]) <= now for g in rows if (g["season"], g["week"]) == k)]
    if not started:
        return None, (seq[0] if seq else None)
    prev = started[-1]
    later = [k for k in seq if k > prev]
    return prev, (later[0] if later else None)


def tuesday_fallback(games: list[dict], cfg: StrategyConfig) -> datetime:
    tz = ZoneInfo(cfg.display_timezone)
    last = max(utc(g["kickoff"]) for g in games).astimezone(tz)
    days = (1 - last.weekday()) % 7 or 7
    hh, mm = (int(x) for x in cfg.tuesday_fallback_local_time.split(":"))
    return datetime.combine(last.date() + timedelta(days=days), time(hh, mm), tz).astimezone(utc(last).tzinfo)


class WeeklyJobs:
    def __init__(self, store: Store, cfg: StrategyConfig, planner: WeeklyPlanner, inputs, fees=None, parlay=None):
        self.store, self.cfg, self.planner, self.inputs, self.fees = store, cfg, planner, inputs, fees
        self.parlay = parlay

    def _done(self, key: str) -> bool:
        job = self.store.job(key)
        return bool(job and job["status"] == "done")

    def reconcile_settlements(self, rows: list[dict], now: datetime) -> list[dict]:
        """Settle open paper inventory from verified final scores under the plan's settlement rule."""
        engine = self.store.load_engine(self.cfg, self.fees)
        by_id = {g["game_id"]: g for g in rows}
        for tid, plan in sorted(engine.state.plans.items()):
            pos = engine.state.positions.get(tid)
            game = by_id.get(plan.game_id)
            if not pos or pos.qty_open <= 0 or not game or not is_final(game):
                continue
            spec = ContractSpec(venue="polymarket_us", market_slug=plan.market_slug, market_id=None, event_id=None,
                                game_id=plan.game_id, season=plan.season, week=plan.week, home=game["home"],
                                away=game["away"], kickoff=plan.kickoff, market_type=plan.market_type,
                                selected_outcome=plan.selected_outcome, selected_side=plan.selected_side,
                                total_line=plan.total_line,
                                tie_payout=Decimal("0.5") if plan.market_type == "moneyline" else None,
                                settlement_rule_version=plan.settlement_rule_version, tick_size=plan.tick_size,
                                quantity_step=plan.quantity_step, min_quantity=plan.min_quantity)
            payout = settlement_payout(spec, int(game["home_score"]), int(game["away_score"]))
            engine.on_settlement(plan.market_slug, {tid: payout}, now,
                                 f"score-derived:{plan.settlement_rule_version}")
        return self.store.commit(engine, now, lifecycle_alerts(engine._pending))

    def ensure_account(self, now: datetime) -> None:
        engine = self.store.load_engine(self.cfg, self.fees)
        if engine.state.account_id is None:
            engine.open_account("paper-main", now)
            self.store.commit(engine, now)

    async def tick(self, now: datetime) -> list[str]:
        actions = []
        self.ensure_account(now)
        rows = self.inputs.all_games()
        if self.parlay is not None:
            try:
                await asyncio.to_thread(self.parlay.tick, rows, now)
            except Exception:
                logging.getLogger(__name__).exception("Weekly parlay unavailable; will retry")
        prev, nxt = previous_and_next(rows, now)
        if prev is None:
            return actions
        season, week = prev
        games = [g for g in rows if (g["season"], g["week"]) == prev]
        final = all(is_final(g) or is_canceled(g) for g in games)
        report_key = f"report:{self.cfg.strategy_id}:{season}:{week:02d}"
        if final:
            seen = [self.store.observe_final(g["game_id"], now, {"home_score": g["home_score"],
                                                                  "away_score": g["away_score"]})
                    for g in games if is_final(g)]
            ready_at = (max(utc(s) for s in seen) if seen else now) + \
                timedelta(minutes=self.cfg.postgame_ingestion_grace_minutes)
            if now >= ready_at and not self._done(report_key):
                self.reconcile_settlements(rows, now)
                self.publish_report(season, week, now)
                self.store.set_job(report_key, "weekly_report", "done", now)
                actions.append(report_key)
        if nxt is None:
            return actions
        n_season, n_week = nxt
        key = plan_key(self.cfg, n_season, n_week)
        plan_job = f"plan:{key}"
        if self._done(plan_job):
            return actions
        waiting_for = "the final scores from last week" if not final else "last week’s results to finish processing"
        if final and self._done(report_key):
            ready, why = self.inputs.ready_for(n_season, n_week, now)
            if ready:
                await self.planner.run(n_season, n_week, now)
                await self.planner.activate(key, now)
                self.store.set_job(plan_job, "weekly_plan", "done", now)
                actions.append(plan_job)
                return actions
            self.store.set_job(plan_job, "weekly_plan", "waiting_data", now, {"reason": why})
            waiting_for = "this week’s schedule" if why == "no scheduled games" else "fresh predictions for this week"
        fallback = tuesday_fallback(games, self.cfg)
        delayed_key = f"delayed:{key}"
        if now >= fallback and not self._done(delayed_key):
            self.store.enqueue([{"publication_id": delayed_key, "kind": "status", "channel": "picks", "revision": 1,
                                 "payload": {"title": f"⏳ Picks on hold · Week {n_week}, {n_season}",
                                             "description": f"Kirkey’s waiting for {waiting_for}. "
                                                            "The picks will post here when the scan finishes.",
                                             "footer": "Beef Kirkey · NFL paper trading"}}], now)
            self.store.set_job(delayed_key, "delayed_status", "done", now)
            actions.append(delayed_key)
        return actions

    def publish_report(self, season: int, week: int, now: datetime) -> dict:
        engine = self.store.load_engine(self.cfg, self.fees)
        account = engine.account(now)
        start = engine.state.week_open.get(f"{season}-{week}") or engine.state.starting_equity
        week_trades = {tid for tid, p in engine.state.plans.items() if (p.season, p.week) == (season, week)}
        events = [e for e in self.store.ledger_events() if e.get("trade_id") in week_trades]
        self.store.save_account_snapshot("weekly", f"{season}-{week:02d}", account, now)
        report = weekly_report(account, D(start), season, week, events, self.cfg,
                               "Market data outages occurred; see /health." if engine.state.outages else None)
        self.store.enqueue([report], now)
        return report
