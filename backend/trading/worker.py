"""Persistent paper-trading worker: python -m backend.trading.worker

Runs independently of any browser tab or Discord interaction. Each loop:
records books for active trades (every ``active_poll_seconds``) and for the
current plan's other candidates (every ``idle_poll_seconds``), feeds snapshots
and the clock to the paper engine, applies verified pre-start rescheduling and
observed game starts, journals everything atomically, and every minute runs the
durable weekly job (slate completion, report, plan, Tuesday fallback).

Pausing stops new entries only; exits, deadlines and settlements keep running.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import signal
import time

from .common import iso, utc
from .config import StrategyConfig
from .inputs import TRADING_DIR, LiveInputs
from .jobs import WeeklyJobs
from .paper_engine import ACTIVE_ENTRY
from .parlay import ParlayPublisher
from .planner import WeeklyPlanner
from .reports import lifecycle_alerts, trade_card_publication
from .store import Store
from .venue import MarketRecorder, ReadOnlyVenueClient

logger = logging.getLogger(__name__)
DB_PATH = os.getenv("NFL_PAPER_DB", str(TRADING_DIR / "paper_trading.sqlite3"))
HEALTH_PATH = TRADING_DIR / "worker.health.json"
HEARTBEAT_INTERVAL_SECONDS = 15


def trade_status(engine, tid: str) -> str:
    o, pos = engine.state.orders.get(tid), engine.state.positions.get(tid)
    if pos and pos.qty_open > 0:
        if o and o.status in ACTIVE_ENTRY:
            return "PARTIALLY_FILLED"
        return pos.status
    if pos and pos.qty_bought > 0:
        return "CLOSED"
    return o.status if o else "PROPOSED"


class Worker:
    def __init__(self, store: Store, cfg: StrategyConfig, venue: ReadOnlyVenueClient, inputs: LiveInputs):
        self.store, self.cfg, self.venue, self.inputs = store, cfg, venue, inputs
        self.recorder = MarketRecorder(venue, store, cfg.active_poll_seconds, cfg.idle_poll_seconds)
        self.planner = WeeklyPlanner(store, cfg, inputs, progress=self._planning_progress)
        self.jobs = WeeklyJobs(store, cfg, self.planner, inputs, parlay=ParlayPublisher(store))
        self.engine = store.load_engine(cfg)
        self.last_jobs = 0.0
        self.last_schedule = 0.0
        self.status = {"started_at": iso(datetime.now(timezone.utc)), "last_loop": None, "last_jobs": None,
                       "last_error": None, "last_heartbeat": None, "phase": "starting", "planning": None}

    def _write_health(self):
        self.status["feed_errors"] = dict(self.recorder.errors)
        self.status["venue_error"] = self.venue.last_error
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = HEALTH_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.status))
        temporary.replace(HEALTH_PATH)

    def _planning_progress(self, progress):
        self.status["planning"] = progress
        self._write_health()

    async def _heartbeat(self, stop):
        # Network waits during a full-slate scan can exceed the health-check
        # timeout. Report process liveness separately from completed work.
        while not stop.is_set():
            self.status["last_heartbeat"] = iso(datetime.now(timezone.utc))
            self._write_health()
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    def _idle_slugs(self) -> set:
        plan = self.store.latest_plan(self.cfg.strategy_id)
        if not plan:
            return set()
        return {d["market_slug"] for d in plan["payload"]["decisions"] if d.get("market_slug")}

    def _commit(self, now):
        events = self.engine._pending[:]
        outbox = lifecycle_alerts(events)
        touched = sorted({e["trade_id"] for e in events if e.get("trade_id")})
        for tid in touched:
            plan = self.engine.state.plans.get(tid)
            if plan:
                n = sum(1 for k in self.engine.state.seen if k.split(":")[1:2] == [tid])
                outbox.append(trade_card_publication(plan, trade_status(self.engine, tid), None, self.cfg,
                                                     revision=2 + n))
        try:
            self.store.commit(self.engine, now, outbox, write_memory=True)
        except Exception:
            # State may be ahead of the journal: rebuild from what was durably stored.
            self.engine = self.store.load_engine(self.cfg)
            raise

    async def loop_once(self):
        self.status["phase"] = "polling_books"
        now = datetime.now(timezone.utc)
        self.engine = self.store.load_engine(self.cfg)  # pick up activations made by the planner
        active = {p.market_slug for tid, p in self.engine.state.plans.items()
                  if trade_status(self.engine, tid) not in ("CLOSED", "CANCELED", "EXPIRED", "INVALIDATED")}
        snaps = await self.recorder.poll(active, self._idle_slugs() - active)
        for snap in sorted(snaps, key=lambda s: (s.received_at, s.slug)):
            self.engine.on_snapshot(snap)
        self.engine.on_clock(now)
        if time.monotonic() - self.last_schedule >= 60:
            self.last_schedule = time.monotonic()
            self._schedule_events(now)
        self._commit(now)
        if time.monotonic() - self.last_jobs >= 60:
            self.last_jobs = time.monotonic()
            self.status["phase"] = "weekly_jobs"
            self._write_health()
            await self.jobs.tick(now)
            self.status["last_jobs"] = iso(datetime.now(timezone.utc))
        self.status["last_loop"] = iso(datetime.now(timezone.utc))
        self.status["phase"] = "idle"
        self._write_health()

    def _schedule_events(self, now):
        try:
            rows = {g["game_id"]: g for g in self.inputs.all_games()}
        except Exception as exc:
            self.status["last_error"] = f"schedule unavailable ({type(exc).__name__})"
            return
        for tid, plan in sorted(self.engine.state.plans.items()):
            g = rows.get(plan.game_id)
            if not g:
                continue
            status = str(g.get("source_status") or "").lower()
            if status == "live" or g.get("home_score") is not None:
                self.engine.on_game_started(plan.game_id, now)
            elif g.get("kickoff_confirmed") and utc(g["kickoff"]) != plan.kickoff and now < plan.kickoff:
                self.engine.on_kickoff_change(plan.game_id, utc(g["kickoff"]), now)

    async def run(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        heartbeat = asyncio.create_task(self._heartbeat(stop))
        try:
            while not stop.is_set():
                if heartbeat.done():
                    await heartbeat  # Surface a failed heartbeat writer.
                try:
                    self.status["last_error"] = None
                    await self.loop_once()
                except Exception as exc:
                    self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                    self.status["phase"] = "error"
                    logger.exception("Paper worker loop failed; state reloaded from the journal")
                    self._write_health()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.cfg.active_poll_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            stop.set()
            try:
                await heartbeat
            finally:
                await self.venue.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = StrategyConfig.from_env()
    store = Store(DB_PATH)
    venue = ReadOnlyVenueClient(min_interval=float(cfg.min_request_interval_seconds))
    worker = Worker(store, cfg, venue, LiveInputs(venue, store))
    engine = worker.engine
    if engine.state.account_id is None:
        engine.open_account("paper-main", datetime.now(timezone.utc))
        store.commit(engine, datetime.now(timezone.utc))
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
