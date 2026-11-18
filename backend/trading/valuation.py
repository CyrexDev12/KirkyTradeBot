"""Payout-correct contract values from the two independent sports models.

The gradient-boosting (GB) classifier and the current-season Monte Carlo (MC)
remain separate. Each adapter returns its own fair value, lower/upper bounds,
evidence status and provenance. Nothing here averages the two models, and an
unavailable MC forecast is never replaced with GB output.

Lower bounds come from documented uncertainty treatments, not from MC draw
counts: a 50,000-draw bootstrap from one fixed fit measures simulation noise,
not model-estimation error.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
import math

from .common import ONE, ZERO, D

GB = "GB"
MC = "MC"


@dataclass(frozen=True)
class UncertaintyProfile:
    """Symmetric half-width added around a model's payout value."""
    provider: str
    market_type: str
    half_width: Decimal
    method: str
    evaluated_games: int
    source: str


@dataclass(frozen=True)
class TieEstimate:
    """Separately justified final-tie probability for the GB payout adapter."""
    rate: Decimal
    low: Decimal
    high: Decimal
    games: int
    ties: int
    source: str


@dataclass(frozen=True)
class ModelValuation:
    provider: str
    status: str  # "available" | "unavailable"
    fair_value: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    forecast_id: str | None = None
    model_version: str | None = None
    as_of: str | None = None
    uncertainty_method: str | None = None
    reason: str | None = None
    code: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status == "available" and self.lower is not None

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("fair_value", "lower", "upper"):
            data[key] = str(data[key]) if data[key] is not None else None
        return data


def unavailable(provider: str, reason: str, code: str | None = None, **raw) -> ModelValuation:
    code = code or ("MC_UNAVAILABLE" if provider == MC else "GB_UNAVAILABLE")
    return ModelValuation(provider=provider, status="unavailable", reason=reason, code=code, raw=raw)


def _clip(x: Decimal) -> Decimal:
    return min(ONE, max(ZERO, x))


def wilson(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 1.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def tie_estimate_from_games(games: list[dict], source: str) -> TieEstimate | None:
    """Empirical regular-season final-tie rate with a Wilson 95% range.

    Uses completed games only. This is a base rate independent of the MC fit, so
    the GB adapter does not borrow MC tie mass.
    """
    done = [g for g in games if g.get("home_score") is not None and g.get("away_score") is not None]
    if len(done) < 500:
        return None
    ties = sum(1 for g in done if g["home_score"] == g["away_score"])
    low, high = wilson(ties, len(done))
    return TieEstimate(rate=D(round(ties / len(done), 6)), low=D(round(low, 6)), high=D(round(high, 6)),
                       games=len(done), ties=ties, source=source)


def half_width_from_calibration(bins: list[dict], provider: str, market_type: str, source: str,
                                min_bin_games: int = 20, min_total_games: int = 200) -> UncertaintyProfile | None:
    """Calibration-error band v1.

    For every calibration bin with at least ``min_bin_games`` games, take the
    absolute predicted-minus-observed gap plus a binomial 95% sampling allowance
    and use the count-weighted mean as a symmetric half-width. This is a policy
    treatment of out-of-sample error, not a confidence interval for one game.
    """
    usable = []
    for b in bins or []:
        n = int(b.get("games") or 0)
        if n < min_bin_games or b.get("predicted") is None or b.get("observed") is None:
            continue
        p, o = float(b["predicted"]), float(b["observed"])
        allowance = 1.96 * math.sqrt(max(p * (1 - p), 1e-6) / n)
        usable.append((n, abs(p - o) + allowance))
    total = sum(n for n, _ in usable)
    if total < min_total_games:
        return None
    width = sum(n * w for n, w in usable) / total
    return UncertaintyProfile(provider=provider, market_type=market_type, half_width=D(round(width, 4)),
                              method="calibration-error-band-v1", evaluated_games=total, source=source)


def _bounded(provider, low_fv, high_fv, point, profile, **meta) -> ModelValuation:
    return ModelValuation(provider=provider, status="available", fair_value=point,
                          lower=_clip(low_fv - profile.half_width), upper=_clip(high_fv + profile.half_width),
                          uncertainty_method=f"{profile.method}; half-width {profile.half_width}", **meta)


def gb_moneyline(prediction: dict | None, team: str, home: str, tie_payout: Decimal | None,
                 tie: TieEstimate | None, profile: UncertaintyProfile | None) -> ModelValuation:
    """fair_value_GB = (1 - t) * q + tie_payout * t, evaluated over the tie range [t_low, t_high]."""
    if not prediction or prediction.get("home_probability") is None:
        return unavailable(GB, "No gradient-boosting prediction")
    if prediction.get("kind") not in (None, "live_pregame"):
        return unavailable(GB, "Prediction is not a live pregame artifact", kind=prediction.get("kind"))
    if tie_payout is None:
        return unavailable(GB, "Unsupported tie settlement", "UNSUPPORTED_SETTLEMENT")
    if tie is None:
        return unavailable(GB, "No separately justified tie estimate for the binary GB classifier",
                           "UNSUPPORTED_SETTLEMENT")
    if profile is None:
        return unavailable(GB, "No documented GB moneyline uncertainty profile", "UNSUPPORTED_UNCERTAINTY")
    q_home = D(round(float(prediction["home_probability"]), 6))
    q = q_home if team == home else ONE - q_home
    values = [(ONE - t) * q + tie_payout * t for t in (tie.low, tie.rate, tie.high)]
    meta = dict(model_version=prediction.get("model_version"), as_of=prediction.get("generated_at"),
                forecast_id=f"gb:{prediction.get('model_version')}:{prediction.get('generated_at')}",
                raw={"conditional_win_probability": str(q), "tie_rate": str(tie.rate),
                     "tie_range": [str(tie.low), str(tie.high)], "tie_source": tie.source})
    return _bounded(GB, min(values), max(values), values[1], profile, **meta)


def mc_moneyline(forecast: dict | None, team: str, home: str, tie_payout: Decimal | None,
                 profile: UncertaintyProfile | None) -> ModelValuation:
    if not forecast or forecast.get("status") != "available":
        return unavailable(MC, (forecast or {}).get("unavailable_reason") or "MC forecast unavailable")
    if tie_payout is None:
        return unavailable(MC, "Unsupported tie settlement", "UNSUPPORTED_SETTLEMENT")
    if profile is None:
        return unavailable(MC, "No documented MC moneyline uncertainty profile", "UNSUPPORTED_UNCERTAINTY")
    role = "home" if team == home else "away"
    value = D(round(forecast[f"probability_{role}_win"], 6)) + tie_payout * D(round(forecast["probability_tie"], 6))
    return _bounded(MC, value, value, value, profile, model_version=forecast.get("model_version"),
                    as_of=forecast.get("as_of"),
                    forecast_id=f"mc:{forecast.get('model_version')}:{forecast.get('game_id')}:{forecast.get('as_of')}",
                    raw={"win": forecast[f"probability_{role}_win"], "tie": forecast["probability_tie"]})


def gb_total(over_probability: float | None, outcome: str, prediction: dict | None,
             profile: UncertaintyProfile | None) -> ModelValuation:
    if over_probability is None or not prediction:
        return unavailable(GB, "No GB total probability at this exact line")
    if profile is None:
        return unavailable(GB, "No documented GB total-line probability calibration", "UNSUPPORTED_UNCERTAINTY")
    p = D(round(over_probability, 6))
    value = p if outcome == "over" else ONE - p
    return _bounded(GB, value, value, value, profile, model_version=prediction.get("model_version"),
                    as_of=prediction.get("generated_at"),
                    forecast_id=f"gbt:{prediction.get('model_version')}:{prediction.get('generated_at')}")


def mc_total(probabilities: dict | None, outcome: str, forecast: dict | None,
             profile: UncertaintyProfile | None) -> ModelValuation:
    if not forecast or forecast.get("status") != "available":
        return unavailable(MC, (forecast or {}).get("unavailable_reason") or "MC forecast unavailable")
    if probabilities is None:
        return unavailable(MC, "MC has no probability at this exact half-point line")
    if profile is None:
        return unavailable(MC, "No documented MC total-line uncertainty profile", "UNSUPPORTED_UNCERTAINTY")
    value = D(round(probabilities[outcome], 6))
    return _bounded(MC, value, value, value, profile, model_version=forecast.get("model_version"),
                    as_of=forecast.get("as_of"),
                    forecast_id=f"mc:{forecast.get('model_version')}:{forecast.get('game_id')}:{forecast.get('as_of')}")


def disagreement(gb: ModelValuation, mc: ModelValuation) -> Decimal | None:
    if gb.fair_value is None or mc.fair_value is None:
        return None
    return abs(gb.fair_value - mc.fair_value)
