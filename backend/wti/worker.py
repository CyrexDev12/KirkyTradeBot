"""PAPER_ONLY energy desk worker: one shared read-only data feed, five model accounts.

Every 5 seconds it reads the windows, books and settlement prices once, updates the news
calendar and shock detector, then lets each model's own Worker price, enter, exit and settle
on that same data. Run: ``python -m backend.wti.worker`` (crude, ``data/wti``) or
``python -m backend.natgas.worker`` (natural gas, ``data/natgas``).
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import signal as os_signal
import sqlite3
import time

import httpx

from ..trading.venue import BASE, ReadOnlyVenueClient
from ..updown.feed import Feed
from ..updown.model import sigma_per_second
from ..updown.store import Store
from ..updown.worker import ERRORS, Worker
from . import Rules
from .commodity import WTI, Commodity
from .events import Calendar, Detector, front_month_expiry
from .models import MODEL_INFO, ModelSet

logger = logging.getLogger(__name__)
ANNOUNCE_AHEAD = 1800  # announce scheduled high/medium releases 30 minutes ahead


class SharedFeed:
    """Wraps a Feed so N model workers share each read within a tick (2-second cache)."""

    def __init__(self, feed, clock=time.time, ttl=2.0):
        self.feed, self.clock, self.ttl, self._cache = feed, clock, ttl, {}

    async def _get(self, key, call, *, requested_after=None):
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < self.ttl and (requested_after is None or hit[2] >= requested_after):
            if isinstance(hit[1], Exception):
                raise hit[1]
            return hit[1]
        requested_at = self.clock()
        try:
            value = await call()
        except ERRORS as exc:
            self._cache[key] = (self.clock(), exc, requested_at)
            raise
        self._cache[key] = (self.clock(), value, requested_at)
        if len(self._cache) > 500:
            cutoff = self.clock() - 60
            self._cache = {k: v for k, v in self._cache.items() if v[0] >= cutoff}
        return value

    async def window(self, product, start):
        return await self._get(("window", product.key, start), lambda: self.feed.window(product, start))

    async def settlement(self, product, start):
        return await self._get(("settle", product.key, start), lambda: self.feed.settlement(product, start))

    async def book(self, slug):
        return await self._get(("book", slug), lambda: self.feed.book(slug))

    async def execution_book(self, slug, ready_at):
        return await self._get(("book", slug), lambda: self.feed.book(slug), requested_after=ready_at)

    async def seconds(self, ids, lookback=120):
        return await self._get(("s", tuple(ids), lookback), lambda: self.feed.seconds(ids, lookback))

    async def minutes(self, ids, lookback=7200):
        return await self._get(("m", tuple(ids), lookback), lambda: self.feed.minutes(ids, lookback))

    async def coinbase(self, product_id):
        raise ValueError("no Coinbase market for this product")


class InfoLog:
    """What the desk noticed: detected shocks/switches and announced releases (for Discord)."""

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS info (id INTEGER PRIMARY KEY, at REAL NOT NULL,
                          kind TEXT NOT NULL, name TEXT NOT NULL, impact TEXT, key TEXT UNIQUE)""")

    def add(self, at, kind, name, impact, key):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO info(at,kind,name,impact,key) VALUES(?,?,?,?,?)",
                       (at, kind, name, impact, key))

    def since(self, after_id=0, limit=50):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT id,at,kind,name,impact FROM info WHERE id>? ORDER BY id LIMIT ?",
                              (after_id, limit)).fetchall()
            latest = db.execute("SELECT coalesce(max(id),0) FROM info").fetchone()[0]
        return {"events": [{"id": r[0], "at": datetime.fromtimestamp(r[1], timezone.utc).isoformat(), "kind": r[2],
                            "name": r[3], "impact": r[4]} for r in rows], "latest_id": latest}

    def recent(self, since):
        with closing(sqlite3.connect(self.path)) as db:
            return [{"at": datetime.fromtimestamp(r[0], timezone.utc).isoformat(), "kind": r[1], "name": r[2],
                     "impact": r[3]} for r in db.execute(
                "SELECT at,kind,name,impact FROM info WHERE at>=? AND kind='detected' ORDER BY at DESC", (since,))]


class Desk:
    def __init__(self, feed, settings=None, models=None, directory=None, clock=time.time, calendar=None,
                 commodity: Commodity = WTI):
        self.commodity = commodity
        self.directory = directory or commodity.directory()
        self.clock, self.settings = clock, settings or commodity.settings()
        models = models or commodity.models()
        self.feed = SharedFeed(feed, clock)
        self.calendar = calendar or Calendar(self.directory / "events.json", commodity.recurring, commodity.jumps)
        self.detector = Detector(roll_jump=commodity.roll_jump)
        self.info = InfoLog(self.directory / "info.sqlite3")
        self.rules = Rules.for_commodity(commodity, self.calendar, self.detector)
        self.modelset = ModelSet(self.calendar, self.detector)
        self.workers = {}
        for model_id in models:
            if model_id not in MODEL_INFO:
                raise ValueError(f"Unknown {commodity.name} model {model_id!r}; choose from {', '.join(MODEL_INFO)}")
            store = Store(self.directory / "models" / model_id / "paper.sqlite3")
            self.workers[model_id] = Worker(store, self.feed, settings, clock=clock,
                                            fair_fn=self.modelset.fair_fn(model_id), gate=self.rules.gate,
                                            pre_exit=self.rules.pre_exit,
                                            tag={"model": model_id, "model_name": MODEL_INFO[model_id][0],
                                                 "commodity": commodity.key})
        self.errors = {}

    async def watch(self):
        """Update the detector and the info log before the models trade."""
        now = self.clock()
        ids = list(dict.fromkeys(p.index_id for p in self.settings.products))
        try:
            seconds = (await self.feed.seconds(ids)).get(self.commodity.key) or []
            minutes = (await self.feed.minutes(ids)).get(self.commodity.key) or []
            sigma = sigma_per_second([c for _, c in minutes[-121:]]) if len(minutes) > 31 else None
        except ERRORS as exc:
            self.errors["watch"] = f"{type(exc).__name__}: {exc}"[:200]
            return
        self.errors.pop("watch", None)
        for event in self.detector.update(seconds, sigma, self.calendar):
            self.info.add(event.at, "detected", event.name, event.impact, f"detected:{int(event.at)}:{event.name[:20]}")
        for event in self.calendar.between(now, now + ANNOUNCE_AHEAD):
            if event.impact != "low":
                self.info.add(event.at, "scheduled", event.name, event.impact, f"scheduled:{int(event.at)}:{event.name}")

    async def tick(self):
        await self.watch()
        for model_id, worker in self.workers.items():
            try:
                await worker.tick()
            except Exception:
                logger.exception("%s model %s cycle failed", self.commodity.name, model_id)
                worker.errors["cycle"] = "unexpected error (see logs)"

    def health(self):
        now = self.clock()
        nxt = next((e for e in self.calendar.between(now, now + 7 * 86400) if e.impact != "low"), None)
        label, expiry = front_month_expiry(datetime.fromtimestamp(now, timezone.utc).date(), self.commodity.expiry)
        return {"last_heartbeat": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                "commodity": self.commodity.key, "markets": [p.key for p in self.settings.products],
                "models": list(self.workers), "lead_model": self.commodity.lead(), "errors": {**self.errors, **{f"{m}:{k}": v for m, w in self.workers.items()
                                                                     for k, v in w.errors.items()}},
                "next_news": nxt.to_dict() if nxt else None,
                "front_month": {"contract": label, "last_trading_day": expiry.isoformat()}}


async def serve(commodity: Commodity = WTI):
    directory = commodity.directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "health.json"
    stop = asyncio.Event()
    for signum in (os_signal.SIGTERM, os_signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    headers = {"User-Agent": "nfl-fair-value-paper/1"}
    async with httpx.AsyncClient(base_url=BASE, timeout=10, headers=headers) as pm, \
            httpx.AsyncClient(timeout=8, headers=headers) as other:
        desk = Desk(Feed(ReadOnlyVenueClient(pm, min_interval=0.2), other), commodity=commodity)
        while not stop.is_set():
            try:
                await desk.tick()
            except Exception:
                logger.exception("%s desk cycle failed", commodity.name)
                desk.errors["cycle"] = "unexpected error (see logs)"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(desk.health()))
            tmp.replace(path)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
