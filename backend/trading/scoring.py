"""Versioned 1-10 trade-quality rating.

The rating is a transparent index, not a probability of profit: an 8/10 never
means an 80% chance of making money. Components are normalized with fixed,
absolute functions (never weekly percentile ranks), so a weak slate cannot
produce a "best" 10/10. Missing essential evidence makes the rating
unavailable; it is never defaulted to the 6.5 threshold.

``rating-v1-dev-defaults`` normalization anchors are development placeholders
chosen before any market-path data exists. They must be re-established on
development data and versioned again before rated trades are enabled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

from .common import D

VERSION = "rating-v1-dev-defaults"
WEIGHTS = {"economics": Decimal("0.35"), "execution": Decimal("0.25"), "evidence": Decimal("0.20"),
           "model_support": Decimal("0.10"), "portfolio_fit": Decimal("0.10")}
ANCHORS = {
    "economics_full_return": Decimal("0.20"),  # lower-bound net P&L per committed dollar scoring 1.0
    "execution_max_spread": Decimal("0.05"),  # selected-side spread scoring 0
    "execution_depth_multiple": Decimal("3"),  # visible depth / planned quantity scoring 1.0
    "evidence_full_games": Decimal("100"),
    "evidence_full_interval_width": Decimal("0.30"),
    "support_full_margin": Decimal("0.10"),
    "support_full_disagreement": Decimal("0.15"),
}


def _clip(x) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), D(x)))


@dataclass(frozen=True)
class RatingInputs:
    lower_bound_return: Decimal | None  # conservative net P&L / committed dollars
    spread: Decimal | None
    entry_depth: Decimal | None
    exit_depth: Decimal | None
    quantity: Decimal | None
    quote_age_seconds: float | None
    max_quote_age_seconds: int
    evidence_games: int | None
    censored_fraction: Decimal | None
    target_interval_width: Decimal | None
    gb_margin: Decimal | None  # lower GB value - all-in entry cost per contract
    mc_margin: Decimal | None
    model_disagreement: Decimal | None
    planned_loss_after: Decimal | None  # aggregate planned loss including this trade
    planned_loss_cap: Decimal | None


@dataclass(frozen=True)
class Rating:
    value: Decimal | None
    components: dict
    version: str = VERSION
    missing: tuple = ()

    def to_dict(self):
        d = asdict(self)
        d["value"] = str(self.value) if self.value is not None else None
        d["components"] = {k: (str(v) if v is not None else None) for k, v in self.components.items()}
        return d


def components(x: RatingInputs) -> dict:
    a = b = c = d = e = None
    if x.lower_bound_return is not None:
        a = _clip(x.lower_bound_return / ANCHORS["economics_full_return"])
    if None not in (x.spread, x.entry_depth, x.exit_depth, x.quantity, x.quote_age_seconds) and x.quantity > 0:
        spread = _clip(1 - x.spread / ANCHORS["execution_max_spread"])
        depth = _clip(min(x.entry_depth, x.exit_depth) / (x.quantity * ANCHORS["execution_depth_multiple"]))
        fresh = _clip(1 - D(x.quote_age_seconds) / D(x.max_quote_age_seconds))
        b = Decimal("0.4") * spread + Decimal("0.4") * depth + Decimal("0.2") * fresh
    if None not in (x.evidence_games, x.censored_fraction, x.target_interval_width):
        c = (_clip(D(x.evidence_games) / ANCHORS["evidence_full_games"]) * _clip(1 - x.censored_fraction)
             * _clip(1 - x.target_interval_width / ANCHORS["evidence_full_interval_width"]))
    if None not in (x.gb_margin, x.mc_margin, x.model_disagreement):
        d = (_clip(min(x.gb_margin, x.mc_margin) / ANCHORS["support_full_margin"])
             * _clip(1 - x.model_disagreement / ANCHORS["support_full_disagreement"]))
    if None not in (x.planned_loss_after, x.planned_loss_cap) and x.planned_loss_cap > 0:
        e = _clip(1 - x.planned_loss_after / x.planned_loss_cap)
    return {"economics": a, "execution": b, "evidence": c, "model_support": d, "portfolio_fit": e}


def combine(parts: dict) -> Rating:
    missing = tuple(k for k, v in parts.items() if v is None)
    if missing:
        return Rating(value=None, components=parts, missing=missing)
    score = sum((WEIGHTS[k] * D(v) for k, v in parts.items()), Decimal("0"))
    value = (1 + 9 * score).quantize(Decimal("0.1"), ROUND_HALF_UP)
    return Rating(value=value, components=parts)


def rate(x: RatingInputs) -> Rating:
    return combine(components(x))
