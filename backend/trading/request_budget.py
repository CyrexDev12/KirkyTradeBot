"""Coordinate public Polymarket request starts and cooldowns across VM processes."""

from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
import math
import os
from pathlib import Path
import sqlite3
import time


# Conservative local ceilings, independent of the exchange's reported quota.
PUBLIC_INTERVAL = 0.125
LIVE_BACKGROUND_INTERVAL = 0.50
EXPOSURE_BACKGROUND_INTERVAL = 0.75
LIVE_NFL_INTERVAL = 5.0
EXPOSURE_NFL_INTERVAL = 8.0
NFL_THROTTLE_COOLDOWN = 20.0
WTI_SIGNAL_INTERVAL = 0.40
PRIORITIES = ("background", "nfl_paper", "wti_signal", "live", "exposure")


def retry_after_seconds(header: str | None, now: float) -> float | None:
    """Read a Retry-After delay or HTTP date, ignoring invalid values."""
    if not header:
        return None
    try:
        delay = float(header)
    except ValueError:
        try:
            date = parsedate_to_datetime(header)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - now
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return max(1.0, delay) if math.isfinite(delay) else None


class RequestBudget:
    """One dispatcher budget per VM, backed by a shared SQLite database.

    Background workers retain normal pacing when the pilot is off. A renewable
    lease reserves slots between background requests while the pilot is armed,
    and reserves more while a live position or an unresolved order exists. NFL
    paper traffic gets a stricter cap and WTI signal data gets dedicated room.
    Requests already in flight are unaffected when another process learns of a 429.
    """

    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS public_budget (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_request REAL NOT NULL DEFAULT 0,
                    last_background REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    consecutive_429 INTEGER NOT NULL DEFAULT 0,
                    live_until REAL NOT NULL DEFAULT 0,
                    exposure_until REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO public_budget(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS priority_lane (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_nfl_paper REAL NOT NULL DEFAULT 0,
                    last_wti_signal REAL NOT NULL DEFAULT 0,
                    nfl_resume_at REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO priority_lane(id) VALUES(1);
            """)

    @classmethod
    def from_env(cls) -> RequestBudget | None:
        path = os.getenv("NFL_VENUE_BUDGET_DB")
        return cls._for_path(Path(path).resolve()) if path else None

    @classmethod
    @lru_cache(maxsize=8)
    def _for_path(cls, path: Path) -> RequestBudget:
        return cls(path)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=0.25, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _state(db):
        return db.execute("""SELECT public_budget.*, last_nfl_paper, last_wti_signal, nfl_resume_at
            FROM public_budget JOIN priority_lane USING (id) WHERE id=1""").fetchone()

    def _reserve(self, priority: str, deadline: float | None) -> float | None:
        """Return 0 for a permit, a wait duration, or None for an expired deadline."""
        with self._write() as db:
            now = self.clock()
            if deadline is not None and now >= deadline:
                return None
            state = self._state(db)
            exposed = state["exposure_until"] > now
            live = exposed or state["live_until"] > now
            background = priority in ("background", "nfl_paper") or (priority == "wti_signal" and not live)
            earliest = max(state["last_request"] + PUBLIC_INTERVAL, state["cooldown_until"])
            if background:
                if exposed:
                    interval = EXPOSURE_BACKGROUND_INTERVAL
                elif live:
                    interval = LIVE_BACKGROUND_INTERVAL
                else:
                    interval = PUBLIC_INTERVAL
                earliest = max(earliest, state["last_background"] + interval)
            if live and priority == "nfl_paper":
                interval = EXPOSURE_NFL_INTERVAL if exposed else LIVE_NFL_INTERVAL
                earliest = max(earliest, state["last_nfl_paper"] + interval, state["nfl_resume_at"])
            elif live and priority == "wti_signal":
                earliest = max(earliest, state["last_wti_signal"] + WTI_SIGNAL_INTERVAL)
            if deadline is not None and earliest >= deadline:
                return None
            if earliest > now:
                return earliest - now
            if background:
                db.execute("UPDATE public_budget SET last_request=?, last_background=? WHERE id=1", (now, now))
            else:
                db.execute("UPDATE public_budget SET last_request=? WHERE id=1", (now,))
            if priority == "nfl_paper":
                db.execute("UPDATE priority_lane SET last_nfl_paper=? WHERE id=1", (now,))
            elif priority == "wti_signal":
                db.execute("UPDATE priority_lane SET last_wti_signal=? WHERE id=1", (now,))
            return 0.0

    async def acquire(self, priority: str = "background", deadline: float | None = None) -> bool:
        """Wait for a public request slot, bounded by an optional Unix deadline."""
        if priority not in PRIORITIES:
            raise ValueError(f"Unknown public request priority {priority!r}")
        while True:
            try:
                wait = await asyncio.to_thread(self._reserve, priority, deadline)
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                wait = 0.05
            if wait is None:
                return False
            if not wait:
                return True
            if deadline is not None and self.clock() >= deadline:
                return False
            # Recheck periodically so a changed lease is reflected promptly.
            await asyncio.sleep(min(wait, 0.25))

    def live_priority_active(self) -> bool:
        """Return whether the armed pilot or its open exposure reserves headroom."""
        with closing(self._connect()) as db:
            state = db.execute("SELECT MAX(live_until, exposure_until) FROM public_budget WHERE id=1").fetchone()
            return state[0] > self.clock()

    def snapshot(self) -> dict:
        """Summarize request pacing state without printing venue credentials."""
        with closing(self._connect()) as db:
            state = self._state(db)
        now = self.clock()
        enabled = state["live_until"] > now
        exposed = state["exposure_until"] > now
        extra_cooldown = max(0.0, state["nfl_resume_at"] - max(now, state["cooldown_until"]))
        return {
            "live_enabled": enabled,
            "live_exposure": exposed,
            "cooldown_seconds": round(max(0.0, state["cooldown_until"] - now), 3),
            "nfl_paper_extra_cooldown_seconds": round(extra_cooldown, 3) if enabled or exposed else 0.0,
            "last_public_request_at": state["last_request"] or None,
        }

    def observe_rate_limit(self, retry_after: str | None = None, priority: str = "background") -> float:
        """Broadcast a venue cooldown and avoid counting one HTTP 429 burst repeatedly."""
        with self._write() as db:
            now = self.clock()
            state = self._state(db)
            existing, count = state["cooldown_until"], state["consecutive_429"]
            explicit_delay = retry_after_seconds(retry_after, now)
            if existing <= now:
                # Start from one second again after a quiet period.
                count = min(6, count + 1) if existing and now - existing <= 30 else 1
                delay = explicit_delay if explicit_delay is not None else min(30.0, 2.0 ** (count - 1))
                existing = now + delay
            elif explicit_delay is not None:
                existing = max(existing, now + explicit_delay)
            db.execute("UPDATE public_budget SET cooldown_until=?, consecutive_429=? WHERE id=1", (existing, count))
            if priority == "nfl_paper" and max(state["live_until"], state["exposure_until"]) > now:
                db.execute("UPDATE priority_lane SET nfl_resume_at=MAX(nfl_resume_at, ?) WHERE id=1",
                           (existing + NFL_THROTTLE_COOLDOWN,))
            return existing

    def set_live_mode(self, enabled: bool, exposure: bool = False, lease_seconds: float = 30) -> None:
        """Renew Oil D's priority or release it immediately when the worker is flat and off."""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Request priority lease must have a positive finite duration")
        with self._write() as db:
            until = self.clock() + lease_seconds
            db.execute("UPDATE public_budget SET live_until=?, exposure_until=? WHERE id=1",
                       (until if enabled else 0, until if exposure else 0))
