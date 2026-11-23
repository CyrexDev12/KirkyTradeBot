"""PAPER_ONLY WTI crude oil desk: Polymarket US "WTI Crude Up or Down" 15-minute, hourly and
daily windows, run on the shared Up/Down engine with five competing fair-value models, each
with its own $100 paper account (see ``models.py``), plus scheduled-news and price-shock
awareness (``events.py``). Journals live in ``data/wti``.

Settings (``WTI_*``; defaults match the gold desk unless noted):

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
from ..updown.strategy import Settings

SETTINGS = Settings.from_env(prefix="WTI")
MODELS = tuple(m.strip().upper() for m in os.getenv("WTI_MODELS", "A,B,C,D,E").split(",") if m.strip())
LEAD_MODEL = os.getenv("WTI_LEAD_MODEL", "E").upper()


def data_directory() -> Path:
    default = Path(__file__).resolve().parents[2] / "data"
    return Path(os.getenv("NFL_DATA_DIR", default)) / "wti"


def model_directory(model_id: str) -> Path:
    return data_directory() / "models" / model_id


@dataclass
class Rules:
    """Entry gates and the pre-news exit, shared by every model."""
    calendar: object
    detector: object
    hold_before: float = float(os.getenv("WTI_NEWS_HOLD_BEFORE", 120))
    hold_after: float = float(os.getenv("WTI_NEWS_HOLD_AFTER", 60))
    pre_news_exit: float = float(os.getenv("WTI_PRE_NEWS_EXIT", 180))
    shock_cooldown: float = float(os.getenv("WTI_SHOCK_COOLDOWN", 60))
    roll_cooldown: float = float(os.getenv("WTI_ROLL_COOLDOWN", 1800))
    participation: float = 0.5

    def gate(self, product, window, now):
        for e in self.calendar.between(now - self.hold_after, now + self.hold_before):
            if e.impact == "high":
                return f"holding off: {e.name} at {e.to_dict()['at_et']}"
        shock = self.detector.last_shock()
        if shock is not None and 0 <= now - shock < self.shock_cooldown:
            return "holding off: price shock moments ago"
        roll = self.detector.last_roll()
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
        sold, proceeds, fee = 0, 0.0, 0.0
        for price, displayed in book.side_bids(trade["side"]):
            if price - taker_fee(price, theta) < floor:
                break
            take = min(trade["remaining"] - sold, int(displayed * self.participation))
            if take <= 0:
                continue
            sold += take
            proceeds += price * take
            fee += taker_fee(price, theta) * take
            if sold >= trade["remaining"]:
                break
        if sold <= 0:
            return None
        return {"kind": "pre_news", "quantity": sold, "price": proceeds / sold, "gross": proceeds, "fee": fee,
                "fair_side": None, "news": upcoming[0].name}
