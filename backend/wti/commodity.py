"""The energy futures the desk trades. Everything else (models, news rules, worker, API,
Discord) is shared; a commodity only changes its market, news calendar and thresholds."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from ..updown.strategy import Settings
from .events import NATGAS_JUMP, NATGAS_RECURRING, WTI_RECURRING, cl_last_trade, ng_last_trade


@dataclass(frozen=True)
class Commodity:
    key: str  # index symbol, slug prefix and data folder: "wti" | "natgas"
    name: str  # "WTI crude" | "Natural gas"
    short: str  # Discord desk name: "Oil" | "Gas"
    emoji: str
    env: str  # settings prefix: WTI_* | NATGAS_*
    tracker: str  # Chief Beef Kirkey desk ids: oil-A.. | gas-A..
    api: str  # /api/<api>
    recurring: tuple = field(repr=False)
    jumps: dict | None = None  # std of the move a release adds, by impact (None = crude defaults)
    roll_jump: float = 0.008  # 1-minute jump with no news treated as a possible contract switch
    expiry: object = cl_last_trade
    decimals: int = 2

    def settings(self) -> Settings:
        return Settings.from_env(prefix=self.env)

    def models(self) -> tuple:
        return tuple(m.strip().upper() for m in os.getenv(f"{self.env}_MODELS", "A,B,C,D,E").split(",") if m.strip())

    def lead(self) -> str:
        return os.getenv(f"{self.env}_LEAD_MODEL", "E").upper()

    def directory(self) -> Path:
        default = Path(__file__).resolve().parents[2] / "data"
        return Path(os.getenv("NFL_DATA_DIR", default)) / self.key

    def price(self, value) -> str:
        return f"${value:,.{self.decimals}f}"


WTI = Commodity("wti", "WTI crude", "Oil", "🛢️", "WTI", "oil", "wti", WTI_RECURRING)
NATGAS = Commodity("natgas", "Natural gas", "Gas", "🔥", "NATGAS", "gas", "natgas", NATGAS_RECURRING,
                   jumps=NATGAS_JUMP, roll_jump=0.015, expiry=ng_last_trade, decimals=4)
COMMODITIES = {c.key: c for c in (WTI, NATGAS)}
