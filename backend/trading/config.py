"""Versioned paper-strategy configuration.

Every numerical value here is an initial experiment setting chosen in the
design brief. None is an optimized parameter or evidence of profitability.
Values can be overridden with ``NFL_PAPER_<FIELD>`` environment variables; the
configuration hash is stored with every plan, decision and validation run.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal
import os

from .common import D, digest

PREGAME_ONLY = "PREGAME_ONLY"
FROZEN_THESIS_PRICE_EXITS = "FROZEN_THESIS_PRICE_EXITS"
HOLD_POLICIES = (PREGAME_ONLY, FROZEN_THESIS_PRICE_EXITS)
CONSERVATIVE_OBSERVABLE_BOOK = "CONSERVATIVE_OBSERVABLE_BOOK"


@dataclass(frozen=True)
class StrategyConfig:
    strategy_id: str = "nfl-dual-model-price-scalp"
    mode: str = "PAPER_ONLY"
    starting_equity_usd: Decimal = Decimal("100.00")
    display_timezone: str = "America/Phoenix"
    min_trade_rating: Decimal = Decimal("6.5")
    min_valuation_margin: Decimal = Decimal("0.02")
    max_open_positions: int = 3
    max_active_trades_per_game: int = 1
    max_funded_loss_fraction_per_game: Decimal = Decimal("0.15")
    max_total_committed_fraction: Decimal = Decimal("0.40")
    max_aggregate_planned_loss_fraction: Decimal = Decimal("0.05")
    risk_fraction_6_5_to_7_4: Decimal = Decimal("0.01")
    risk_fraction_7_5_to_8_4: Decimal = Decimal("0.02")
    risk_fraction_8_5_to_10: Decimal = Decimal("0.03")
    weekly_new_entry_pause_drawdown: Decimal = Decimal("0.08")
    account_review_drawdown: Decimal = Decimal("0.20")
    entry_expiry_minutes_before_kickoff: int = 10
    pregame_exit_minutes_before_kickoff: int = 5
    frozen_thesis_exit_hours_after_kickoff: int = 4
    postgame_ingestion_grace_minutes: int = 30
    tuesday_fallback_local_time: str = "08:00"
    hold_policy: str = FROZEN_THESIS_PRICE_EXITS
    rated_trades_require_validation: bool = True
    paper_fill_mode: str = CONSERVATIVE_OBSERVABLE_BOOK
    live_nfl_reforecasting: bool = False
    real_order_execution: bool = False
    # Execution-simulation assumptions (operating assumptions to validate).
    latency_seconds: int = 2
    max_quote_age_seconds: int = 20
    outage_gap_seconds: int = 30
    entry_participation_cap: Decimal = Decimal("0.50")
    stop_stress_price_delta: Decimal = Decimal("0.005")
    active_poll_seconds: int = 5
    idle_poll_seconds: int = 60
    min_request_interval_seconds: Decimal = Decimal("0.6")
    # Fee profile: explicitly chosen conservative small-account taker profile.
    fee_profile_id: str = "polymarket-us-taker-lowest-volume-2026-11-14"
    # Frozen candidate grid (in ticks) and ranking objective identity.
    grid_entry_ticks_below_ask: tuple = (0, 1, 2)
    grid_target_ticks_above_entry: tuple = (5, 10, 15)
    grid_stop_ticks_below_entry: tuple = (3, 4, 6)
    ranking_objective: str = "lower_bound_net_pnl_per_committed_dollar-v1"
    rating_version: str = "rating-v1-dev-defaults"
    # Market-path readiness policy. Not a claim that these counts prove anything.
    readiness_min_games: int = 30
    readiness_min_weeks: int = 6
    readiness_min_filled: int = 30
    readiness_max_censored_fraction: Decimal = Decimal("0.10")
    readiness_lower_quantile: Decimal = Decimal("0.10")
    readiness_bootstrap_draws: int = 400
    max_history_interval_seconds: int = 120
    supported_market_types: tuple = ("moneyline", "total")
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self):
        if self.mode != "PAPER_ONLY" or self.real_order_execution:
            raise ValueError("This subsystem only runs in PAPER_ONLY mode with REAL_ORDER_EXECUTION=false")
        if self.live_nfl_reforecasting:
            raise ValueError("Live NFL reforecasting is not supported; forecasts freeze at kickoff")
        if self.hold_policy not in HOLD_POLICIES:
            raise ValueError(f"Unknown hold policy {self.hold_policy}")
        if self.paper_fill_mode != CONSERVATIVE_OBSERVABLE_BOOK:
            raise ValueError("Only the conservative observable-book fill mode is implemented")

    @property
    def version(self) -> str:
        data = asdict(self)
        data.pop("extra", None)
        return digest(data)

    def risk_fraction(self, rating: Decimal) -> Decimal:
        rating = D(rating)
        if rating >= Decimal("8.5"):
            return self.risk_fraction_8_5_to_10
        if rating >= Decimal("7.5"):
            return self.risk_fraction_7_5_to_8_4
        if rating >= Decimal("6.5"):
            return self.risk_fraction_6_5_to_7_4
        return Decimal("0")

    @classmethod
    def from_env(cls, env=None, **overrides) -> "StrategyConfig":
        env = os.environ if env is None else env
        values = {}
        for item in fields(cls):
            raw = env.get(f"NFL_PAPER_{item.name.upper()}")
            if raw is None or item.name == "extra":
                continue
            default = getattr(cls, item.name, None) if not callable(getattr(cls, item.name, None)) else None
            if isinstance(default, bool):
                values[item.name] = raw.strip().lower() in {"1", "true", "yes"}
            elif isinstance(default, int):
                values[item.name] = int(raw)
            elif isinstance(default, Decimal):
                values[item.name] = D(raw)
            elif isinstance(default, tuple):
                values[item.name] = tuple(int(x) if x.strip().lstrip("-").isdigit() else x.strip()
                                          for x in raw.split(",") if x.strip())
            else:
                values[item.name] = raw
        values.update(overrides)
        return cls(**values)

    def with_overrides(self, **values) -> "StrategyConfig":
        return replace(self, **values)
