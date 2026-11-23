"""Information the energy desks watch: scheduled releases that move the price, and moves in the
settlement price that look like news or a futures-contract switch.

Scheduled for **WTI crude** (New York time; edit or extend with ``data/wti/events.json``):

* EIA Weekly Petroleum Status Report — Wednesday 10:30 AM (high impact)
* API weekly crude inventory — Tuesday 4:30 PM (medium)
* Baker Hughes rig count — Friday 1:00 PM (low)
* US jobs report — first Friday of the month 8:30 AM (high)
* CME crude settlement window — weekdays 2:28–2:30 PM (low)

Scheduled for **natural gas** (``data/natgas/events.json``):

* EIA Weekly Natural Gas Storage Report — Thursday 10:30 AM (high)
* NOAA 6–10 / 8–14 day weather outlooks — daily 3:00 PM (medium)
* Baker Hughes rig count — Friday 1:00 PM (low)
* CME natural gas settlement window — weekdays 2:28–2:30 PM (low)

``events.json`` adds one-offs (OPEC+ meetings, CPI, FOMC, holiday-shifted reports), known
contract-switch days, and can skip a recurring release on a holiday::

    {"events": [{"at": "2026-11-30T09:00:00-05:00", "name": "OPEC+ meeting", "impact": "high"}],
     "roll_dates": ["2026-11-19"],
     "skip": ["2026-11-25 EIA Weekly Petroleum Status Report"]}

Detected (from the 1-second settlement price):

* **Shock** — a 10-second move of 6+ typical 10-second moves (news hitting the market).
* **Possible contract switch** — a one-minute jump of 0.8%+ with no scheduled release nearby.
  Chainlink moves to the next futures month on its own schedule and does not adjust the
  price, so a window that straddles the switch compares two different contracts.

There is no live news feed: unscheduled headlines are seen only through the price.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}
# Rough priors for the size of the move a release adds (standard deviation, share of price).
# The event-aware model uses these; the live journal will show whether they need changing.
JUMP = {"high": 0.0035, "medium": 0.0020, "low": 0.0007}

WTI_RECURRING = (
    # (weekday Mon=0, hour, minute, name, impact, only first week of month)
    (2, 10, 30, "EIA Weekly Petroleum Status Report", "high", False),
    (1, 16, 30, "API weekly crude inventory", "medium", False),
    (4, 13, 0, "Baker Hughes rig count", "low", False),
    (4, 8, 30, "US jobs report", "high", True),
    (0, 14, 28, "CME crude settlement window", "low", False),
    (1, 14, 28, "CME crude settlement window", "low", False),
    (2, 14, 28, "CME crude settlement window", "low", False),
    (3, 14, 28, "CME crude settlement window", "low", False),
    (4, 14, 28, "CME crude settlement window", "low", False),
)
RECURRING = WTI_RECURRING

NATGAS_RECURRING = (
    (3, 10, 30, "EIA Weekly Natural Gas Storage Report", "high", False),
    *((d, 15, 0, "NOAA 6-10 / 8-14 day weather outlooks", "medium", False) for d in range(5)),
    (4, 13, 0, "Baker Hughes rig count", "low", False),
    *((d, 14, 28, "CME natural gas settlement window", "low", False) for d in range(5)),
)
# Natural gas moves about twice as much as crude around its releases (Nov 18-20, 2026: typical
# 1-minute moves rose from 11 to 18 basis points in the 10 minutes after the storage report).
NATGAS_JUMP = {"high": 0.0060, "medium": 0.0025, "low": 0.0010}


@dataclass(frozen=True)
class Event:
    at: float  # unix seconds
    name: str
    impact: str = "medium"
    source: str = "scheduled"  # scheduled | file | detected
    jump: float | None = None  # std of the move it adds, share of price

    @property
    def size(self) -> float:
        return self.jump if self.jump is not None else JUMP.get(self.impact, JUMP["medium"])

    def to_dict(self):
        d = asdict(self)
        d["at_iso"] = datetime.fromtimestamp(self.at, timezone.utc).isoformat()
        d["at_et"] = datetime.fromtimestamp(self.at, timezone.utc).astimezone(ET).strftime("%a %b %-d %-I:%M %p ET")
        return d


def cl_last_trade(year: int, month: int) -> date:
    """Last trading day of the NYMEX WTI contract for delivery month (year, month): three business
    days before the 25th of the prior month (or before the last business day before the 25th).
    Weekends only; exchange holidays are not modelled."""
    prior = date(year - 1, 12, 25) if month == 1 else date(year, month - 1, 25)
    d = prior
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    count = 0
    while count < 3:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d


def ng_last_trade(year: int, month: int) -> date:
    """Last trading day of the NYMEX Henry Hub natural gas contract for delivery month (year,
    month): three business days before the first calendar day of that month (weekends only)."""
    d, count = date(year, month, 1), 0
    while count < 3:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d


def front_month_expiry(today: date, rule=cl_last_trade) -> tuple[str, date]:
    """(contract label, last trading day) of the front month on ``today`` under ``rule``."""
    y, m = today.year, today.month
    for _ in range(4):
        if m > 12:
            y, m = y + 1, m - 12
        last = rule(y, m)
        if last >= today:
            return date(y, m, 1).strftime("%b %Y"), last
        m += 1
    raise ValueError("no front month found")


class Calendar:
    def __init__(self, path: Path | None = None, recurring=WTI_RECURRING, jumps=None):
        self.path, self.recurring, self.jumps = path, recurring, jumps
        self._mtime = None
        self.file_events: list[Event] = []
        self.roll_dates: set[str] = set()
        self.skip: set[str] = set()

    def _load(self):
        if not self.path or not self.path.is_file():
            return
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text())
            events = []
            for e in data.get("events", []):
                at = datetime.fromisoformat(e["at"])
                at = at.replace(tzinfo=ET) if at.tzinfo is None else at
                impact = e.get("impact", "medium")
                events.append(Event(at.timestamp(), e["name"], impact, "file",
                                    e.get("jump", (self.jumps or {}).get(impact))))
            self.file_events = events
            self.roll_dates = set(data.get("roll_dates", []))
            self.skip = set(data.get("skip", []))
            self._mtime = mtime
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring unreadable %s: %s", self.path, exc)

    def between(self, start: float, end: float) -> list[Event]:
        """Scheduled events with start <= at <= end, earliest first."""
        self._load()
        out = []
        day = datetime.fromtimestamp(start, timezone.utc).astimezone(ET).date() - timedelta(days=1)
        last = datetime.fromtimestamp(end, timezone.utc).astimezone(ET).date()
        while day <= last:
            for weekday, hour, minute, name, impact, first_week in self.recurring:
                if day.weekday() != weekday or (first_week and day.day > 7):
                    continue
                if f"{day.isoformat()} {name}" in self.skip:
                    continue
                at = datetime.combine(day, dtime(hour, minute), ET).timestamp()
                if start <= at <= end:
                    out.append(Event(at, name, impact, jump=(self.jumps or {}).get(impact)))
            day += timedelta(days=1)
        out += [e for e in self.file_events if start <= e.at <= end]
        return sorted(out, key=lambda e: e.at)

    def is_roll_day(self, now: float) -> bool:
        self._load()
        return datetime.fromtimestamp(now, timezone.utc).astimezone(ET).date().isoformat() in self.roll_dates


@dataclass
class Detector:
    """Watches the 1-second settlement price for shocks and contract-switch jumps."""
    shock_z: float = 6.0
    roll_jump: float = 0.008
    shocks: list = field(default_factory=list)  # (unix time, signed 10s move, z)
    rolls: list = field(default_factory=list)  # (unix time, signed 1-minute move)
    _seen: float = 0.0

    def update(self, seconds: list[tuple[int, float]], sigma: float | None, calendar: Calendar) -> list[Event]:
        """Feed the latest 1-second prices; returns newly detected events."""
        found = []
        if not seconds or len(seconds) < 12:
            return found
        by_t = dict(seconds)
        for t, price in seconds:
            if t <= self._seen:
                continue
            ten, minute = by_t.get(t - 10), by_t.get(t - 60)
            if sigma and ten:
                move = math.log(price / ten)
                z = abs(move) / (sigma * math.sqrt(10))
                if z >= self.shock_z and not (self.shocks and t - self.shocks[-1][0] < 60):
                    self.shocks.append((t, move, z))
                    found.append(Event(t, f"Price shock {move:+.2%} in 10 s", "high", "detected"))
            if minute:
                jump = math.log(price / minute)
                near_news = calendar.between(t - 300, t + 120)
                if abs(jump) >= self.roll_jump and not any(e.impact != "low" for e in near_news) \
                        and not (self.rolls and t - self.rolls[-1][0] < 900):
                    self.rolls.append((t, jump))
                    found.append(Event(t, f"Jump {jump:+.2%} in 1 min with no scheduled news "
                                          "(possible contract switch)", "high", "detected"))
        self._seen = max(t for t, _ in seconds)
        self.shocks = self.shocks[-50:]
        self.rolls = self.rolls[-20:]
        return found

    def last_shock(self) -> float | None:
        return self.shocks[-1][0] if self.shocks else None

    def last_roll(self) -> float | None:
        return self.rolls[-1][0] if self.rolls else None
