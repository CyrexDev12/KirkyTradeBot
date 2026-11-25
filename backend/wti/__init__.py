"""PAPER_ONLY energy futures desks on the shared Up/Down engine: Polymarket US "WTI Crude Up or
Down" (this package, ``data/wti``) and "Natural Gas Up or Down" (``backend.natgas``,
``data/natgas``), each in 15-minute, hourly and daily windows with five competing fair-value
models that each keep their own $100 paper account (``models.py``), plus scheduled-news and
price-shock awareness (``events.py``). ``commodity.py`` holds what differs per commodity.

Settings (``WTI_*`` for crude, ``NATGAS_*`` for gas; defaults match the gold desk unless noted):

* ``WTI_HORIZONS`` [15m,1h,1d] · ``WTI_MODELS`` [A,B,C,D,E] · ``WTI_LEAD_MODEL`` [E] — the
  model whose picks post to the Discord oil channel (all models trade on paper either way)
* ``WTI_NEWS_HOLD_BEFORE`` [120] / ``WTI_NEWS_HOLD_AFTER`` [60] — seconds around a high-impact
  release with no new entries
* ``WTI_PRE_NEWS_EXIT`` [180] — seconds before a high-impact release to sell an open bet if the
  bid at least gets the entry price back (0 = off)
* ``WTI_SHOCK_COOLDOWN`` [60] / ``WTI_ROLL_COOLDOWN`` [1800] — seconds with no new entries after
  a detected price shock / possible contract switch
* plus the shared ``WTI_MIN_EDGE``, ``WTI_MAX_STAKE``, ``WTI_TAKE_PROFIT``, ``WTI_STOP_LOSS``…
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ..updown.model import taker_fee
from ..updown.strategy import sell_bids
from .commodity import COMMODITIES, NATGAS, WTI, Commodity

SETTINGS = WTI.settings()
MODELS = WTI.models()
LEAD_MODEL = WTI.lead()


def data_directory() -> Path:
    return WTI.directory()


def model_directory(model_id: str, commodity: Commodity = WTI) -> Path:
    return commodity.directory() / "models" / model_id


@dataclass
class Rules:
    """Entry gates and the pre-news exit, shared by every model."""
    calendar: object
    detector: object
    hold_before: float = 120
    hold_after: float = 60
    pre_news_exit: float = 180
    shock_cooldown: float = 60
    roll_cooldown: float = 1800
    participation: float = 0.5

    @classmethod
    def for_commodity(cls, commodity: Commodity, calendar, detector, env=None):
        env = os.environ if env is None else env
        get = lambda name, default: float(env.get(f"{commodity.env}_{name}", default))  # noqa: E731
        return cls(calendar, detector, hold_before=get("NEWS_HOLD_BEFORE", 120), hold_after=get("NEWS_HOLD_AFTER", 60),
                   pre_news_exit=get("PRE_NEWS_EXIT", 180), shock_cooldown=get("SHOCK_COOLDOWN", 60),
                   roll_cooldown=get("ROLL_COOLDOWN", 1800))

    def gate(self, product, window, now):
        return self.entry_hold(window, now, shock=self.detector.last_shock(), roll=self.detector.last_roll())

    def entry_hold(self, window, now, *, shock, roll):
        for e in self.calendar.between(now - self.hold_after, now + self.hold_before):
            if e.impact == "high":
                return f"holding off: {e.name} at {e.to_dict()['at_et']}"
        if shock is not None and 0 <= now - shock < self.shock_cooldown:
            return "holding off: price shock moments ago"
        if roll is not None and 0 <= now - roll < self.roll_cooldown:
            return "holding off: possible futures contract switch (price jumped with no news)"
        if self.calendar.is_roll_day(now) or self.calendar.is_roll_day(window.end):
            return "holding off: contract-switch day (set in events.json)"
        return None

    def pre_exit(self, trade, book, now, fair_up):
        """Sell ahead of a high-impact release if the bid gets at least the entry price back."""
        if not self.pre_news_exit or trade["remaining"] <= 0:
            return None
        upcoming = [e for e in self.calendar.between(now, min(trade["window_end_ts"], now + self.pre_news_exit))
                    if e.impact == "high"]
        if not upcoming:
            return None
        theta, floor = trade["theta"], trade["entry_price"] + trade["entry_fee"] / max(trade["quantity"], 1)
        return sell_bids(book.side_bids(trade["side"]), trade["remaining"], theta, self.participation,
                         lambda price: price - taker_fee(price, theta) >= floor,
                         kind="pre_news", news=upcoming[0].name)
