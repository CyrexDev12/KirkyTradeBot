"""Hard eligibility gates, the frozen entry/target/stop grid, and final sizing.

Gates run in the brief's order and each failure carries an explicit code. A
public rating is only computed for a combination that passed every earlier
gate, and a trade is accepted only when the rating is at least the minimum AND
every gate passed: a 9.0 with negative conservative expectancy is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .book import OPEN, BookSnapshot, is_fresh, selected_view
from .common import ONE, ZERO, D, iso, on_tick
from .config import StrategyConfig
from .contracts import ContractSpec, MappingError
from .fees import execution_fee
from .config import FROZEN_THESIS_PRICE_EXITS
from .path_model import AnalogueEstimator, bucket, modeled_estimate
from .paper_engine import TradePlan, deadlines
from .risk import Exposure, size
from .scoring import Rating, RatingInputs, components, combine
from .valuation import ModelValuation, disagreement

# Ordered by the stage at which each gate runs; the furthest stage reached is the primary reason.
CODES = (
    "NO_LISTED_MARKET", "CONTRACT_MAPPING_FAILED", "UNSUPPORTED_MARKET_TYPE", "UNSUPPORTED_SETTLEMENT",
    "ENTRY_WINDOW_CLOSED", "MARKET_NOT_OPEN", "MC_UNAVAILABLE", "GB_UNAVAILABLE", "UNSUPPORTED_UNCERTAINTY",
    "STALE_MARKET_DATA", "UNKNOWN_INCREMENTS", "INSUFFICIENT_LIQUIDITY", "NO_MODEL_CONSENSUS",
    "PLAN_INVALID_FOR_BOOK", "INSUFFICIENT_EXECUTION_HISTORY", "NEGATIVE_NET_EXPECTANCY", "ENTRIES_SUSPENDED",
    "PORTFOLIO_LIMIT", "RATING_UNAVAILABLE", "RATING_BELOW_MINIMUM",
)
# Codes meaning "could not evaluate" rather than "evaluated and unattractive".
EVIDENCE_CODES = {"MC_UNAVAILABLE", "GB_UNAVAILABLE", "UNSUPPORTED_UNCERTAINTY", "INSUFFICIENT_EXECUTION_HISTORY",
                  "STALE_MARKET_DATA", "UNKNOWN_INCREMENTS", "NO_LISTED_MARKET", "UNSUPPORTED_SETTLEMENT",
                  "RATING_UNAVAILABLE"}
STAGE = {code: i for i, code in enumerate(CODES)}


@dataclass
class CandidateInput:
    game: dict
    market_type: str
    selected_outcome: str
    spec: ContractSpec | None
    mapping_error: MappingError | None
    gb: ModelValuation
    mc: ModelValuation
    snapshot: BookSnapshot | None
    now: datetime


@dataclass
class Combo:
    entry: Decimal
    target: Decimal
    stop: Decimal
    bucket: str
    cost_per_contract: Decimal
    gb_margin: Decimal
    mc_margin: Decimal
    estimate: object
    partial_rating: dict = field(default_factory=dict)


@dataclass
class Decision:
    game_id: str
    market_slug: str | None
    market_type: str
    selected_outcome: str
    accepted: bool = False
    codes: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    combos: list = field(default_factory=list)
    plan: TradePlan | None = None
    rating: Rating | None = None
    sizing: dict | None = None
    best: Combo | None = None

    def reject(self, code: str, note: str | None = None):
        if code not in self.codes:
            self.codes.append(code)
        if note:
            self.notes.append(f"{code}: {note}")
        return self

    @property
    def primary_code(self) -> str | None:
        return max(self.codes, key=lambda c: STAGE.get(c, -1)) if self.codes else None

    @property
    def evidence_limited(self) -> bool:
        return bool(self.codes) and self.primary_code in EVIDENCE_CODES

    def to_dict(self, inp: CandidateInput | None = None) -> dict:
        out = {"game_id": self.game_id, "market_slug": self.market_slug, "market_type": self.market_type,
               "selected_outcome": self.selected_outcome, "accepted": self.accepted,
               "rejection_codes": sorted(self.codes, key=lambda c: STAGE.get(c, 99)),
               "primary_code": self.primary_code, "evidence_limited": self.evidence_limited,
               "notes": self.notes[:12], "combinations_considered": len(self.combos),
               "plan": self.plan.to_dict() if self.plan else None,
               "rating": self.rating.to_dict() if self.rating else None, "sizing": self.sizing}
        if inp is not None:
            out["valuations"] = {"GB": inp.gb.to_dict(), "MC": inp.mc.to_dict()}
        return out


def effective_value(v: ModelValuation, cfg: StrategyConfig) -> Decimal:
    """Model low-end value using ``uncertainty_band_scale`` of its documented band."""
    return v.fair_value - cfg.uncertainty_band_scale * (v.fair_value - v.lower)


def floor_tick(price: Decimal, tick: Decimal) -> Decimal:
    from decimal import ROUND_FLOOR
    return (price / tick).to_integral_value(ROUND_FLOOR) * tick


def all_in_cost(entry: Decimal, theta: Decimal) -> Decimal:
    """Per-contract cost at the entry limit for a 100-contract reference execution."""
    return entry + execution_fee(theta, [(entry, D(100))]) / D(100)


def evaluate(inp: CandidateInput, cfg: StrategyConfig, theta: Decimal, estimator: AnalogueEstimator,
             validated_buckets: set | None) -> Decision:
    """Gates 1-9. Returns surviving combos (pre-sizing) on the decision."""
    spec = inp.spec
    d = Decision(game_id=inp.game["game_id"], market_slug=spec.market_slug if spec else None,
                 market_type=inp.market_type, selected_outcome=inp.selected_outcome)
    # 1. Mapping
    if inp.mapping_error is not None:
        return d.reject(inp.mapping_error.code, inp.mapping_error.detail)
    if spec is None:
        return d.reject("NO_LISTED_MARKET")
    if spec.market_type not in cfg.supported_market_types:
        return d.reject("UNSUPPORTED_MARKET_TYPE", spec.market_type)
    entry_expiry, _ = deadlines(spec.kickoff, cfg.hold_policy, cfg)
    if inp.now >= entry_expiry:
        return d.reject("ENTRY_WINDOW_CLOSED")
    # 2. Market open
    if inp.snapshot is None:
        return d.reject("STALE_MARKET_DATA", "no order book observed")
    if inp.snapshot.state != OPEN:
        return d.reject("MARKET_NOT_OPEN", inp.snapshot.state)
    # 3-4. Both forecasts and payout/uncertainty support. MC is never replaced by GB.
    for v in (inp.mc, inp.gb):
        if not v.available:
            d.reject(v.code or f"{v.provider}_UNAVAILABLE", v.reason)
    if d.codes:
        return d
    # 6 (data quality before economics).
    fresh, why = is_fresh(inp.snapshot, inp.now, cfg.max_quote_age_seconds)
    if not fresh:
        return d.reject("STALE_MARKET_DATA", why)
    if not spec.increments_verified:
        return d.reject("UNKNOWN_INCREMENTS", "tick size or quantity step not verified from market metadata")
    view = selected_view(inp.snapshot, spec.selected_side)
    if view.best_ask is None or view.best_bid is None:
        return d.reject("INSUFFICIENT_LIQUIDITY", "one side of the selected book is empty")
    tick = spec.tick_size
    gb_eff, mc_eff = effective_value(inp.gb, cfg), effective_value(inp.mc, cfg)
    max_multiple = cfg.max_target_multiple_pregame if cfg.hold_policy != FROZEN_THESIS_PRICE_EXITS else None
    for k in cfg.grid_entry_ticks_below_ask:
        entry = view.best_ask - tick * k
        if entry <= 0 or not on_tick(entry, tick):
            continue
        cost = all_in_cost(entry, theta)
        gb_margin, mc_margin = gb_eff - cost, mc_eff - cost
        # 5. Both models support the SAME selected contract at this entry after costs.
        if min(gb_margin, mc_margin) < cfg.min_valuation_margin:
            d.combos.append(Combo(entry, entry, entry, "", cost, gb_margin, mc_margin, None))
            d.reject("NO_MODEL_CONSENSUS", f"entry {entry}: GB margin {gb_margin:.4f}, MC margin {mc_margin:.4f}")
            continue
        for t in cfg.grid_target_multiples:
            if max_multiple is not None and D(t) > max_multiple:
                continue
            for s in cfg.grid_stop_fractions:
                target, stop = floor_tick(entry * D(t), tick), floor_tick(entry * D(s), tick)
                if target > cfg.max_target_price:
                    d.reject("PLAN_INVALID_FOR_BOOK", f"{t}x target {target} above the {cfg.max_target_price} cap")
                    continue
                combo = Combo(entry, target, stop, bucket(spec.market_type, cfg.hold_policy, entry, target, stop),
                              cost, gb_margin, mc_margin, None)
                d.combos.append(combo)
                # 7. Explicit, legal plan for the current book.
                if not ZERO < stop < entry < target < ONE:
                    d.reject("PLAN_INVALID_FOR_BOOK", "target/stop outside (0, 1)")
                    continue
                if view.best_bid <= stop:
                    d.reject("PLAN_INVALID_FOR_BOOK", f"best bid {view.best_bid} already at/below stop {stop}")
                    continue
                # 8. Market-path estimator supported for this market type, price regime and horizon.
                est = estimator.estimate(combo.bucket, before=inp.now)
                if not est.supported and cfg.allow_modeled_path_estimates and k == 0 \
                        and cfg.hold_policy == FROZEN_THESIS_PRICE_EXITS:
                    # No recorded history yet: price the trade from the modeled path (labeled as such).
                    est = modeled_estimate(entry, target, stop, min(gb_eff, mc_eff), theta, cfg, combo.bucket)
                combo.estimate = est
                if not est.supported:
                    d.reject("INSUFFICIENT_EXECUTION_HISTORY", "; ".join(est.reasons) or "unsupported")
                    continue
                if est.recorded and cfg.rated_trades_require_validation \
                        and combo.bucket not in (validated_buckets or set()):
                    d.reject("INSUFFICIENT_EXECUTION_HISTORY", "no passing forward-validation artifact for this bucket")
                    continue
                # 9. Conservative net expectancy after costs.
                if est.lower_return is None or est.lower_return <= 0:
                    d.reject("NEGATIVE_NET_EXPECTANCY", f"lower-bound return {est.lower_return}")
                    continue
                combo.partial_rating = {"lower_bound_return": est.lower_return}
    return d


def _rating(combo: Combo, inp: CandidateInput, spec: ContractSpec, cfg: StrategyConfig, quantity: Decimal,
            planned_after: Decimal, cap: Decimal) -> Rating:
    view = selected_view(inp.snapshot, spec.selected_side)
    entry_depth = sum((q for p, q in view.asks if p <= combo.entry), ZERO)
    exit_depth = sum((q for p, q in view.bids if p > combo.stop), ZERO)
    est = combo.estimate
    width = (est.target_interval[1] - est.target_interval[0]) if est.target_interval else None
    return combine(components(RatingInputs(
        lower_bound_return=est.lower_return, spread=view.best_ask - view.best_bid, entry_depth=entry_depth,
        exit_depth=exit_depth, quantity=quantity,
        quote_age_seconds=(inp.now - inp.snapshot.received_at).total_seconds(),
        max_quote_age_seconds=cfg.max_quote_age_seconds, evidence_games=est.games,
        censored_fraction=est.censored_fraction, target_interval_width=width, gb_margin=combo.gb_margin,
        mc_margin=combo.mc_margin, model_disagreement=disagreement(inp.gb, inp.mc),
        planned_loss_after=planned_after, planned_loss_cap=cap)))


def finalize(d: Decision, inp: CandidateInput, cfg: StrategyConfig, theta: Decimal, *, equity: Decimal,
             available_cash: Decimal, exposure: Exposure, suspended: str | None, trade_id: str,
             plan_key: str) -> Decision:
    """Gates 10-11: sizing at the exact quantity, rating, and the frozen plan."""
    spec = inp.spec
    viable = [c for c in d.combos if c.partial_rating]
    if not viable or d.accepted:
        return d
    if suspended:
        return d.reject("ENTRIES_SUSPENDED", suspended)
    viable.sort(key=lambda c: (-c.estimate.lower_return, -min(c.gb_margin, c.mc_margin), c.entry, c.target, -c.stop))
    view = selected_view(inp.snapshot, spec.selected_side)
    cap = cfg.max_aggregate_planned_loss_fraction * equity
    for combo in viable:
        rating, sizing = None, None
        tier_rating = Decimal("10")
        for _ in range(4):  # rating tier and size are mutually dependent; iterate to a fixed point
            sizing = size(rating=tier_rating, equity=equity, available_cash=available_cash, exposure=exposure,
                          game_id=spec.game_id, entry_limit=combo.entry, stop_trigger=combo.stop, theta=theta,
                          selected_asks=list(view.asks), cfg=cfg, step=spec.quantity_step,
                          min_qty=spec.min_quantity or spec.quantity_step)
            if sizing.quantity <= 0:
                break
            rating = _rating(combo, inp, spec, cfg, sizing.quantity, exposure.planned_loss + sizing.planned_loss, cap)
            if rating.value is None or rating.value < cfg.min_trade_rating \
                    or cfg.risk_fraction(rating.value) == cfg.risk_fraction(tier_rating):
                break
            tier_rating = rating.value
        if rating is not None and rating.value is not None and rating.value < cfg.min_trade_rating:
            d.reject("RATING_BELOW_MINIMUM", f"{rating.value} < {cfg.min_trade_rating}")
            continue
        if sizing is None or sizing.quantity <= 0:
            code = "INSUFFICIENT_LIQUIDITY" if sizing and "depth" in sizing.binding else "PORTFOLIO_LIMIT"
            d.reject(code, sizing.binding if sizing else None)
            continue
        if rating is None or rating.value is None:
            d.reject("RATING_UNAVAILABLE", f"missing components: {', '.join(rating.missing) if rating else 'all'}")
            continue
        # Final-size recheck: exact fees at this quantity must keep both model margins.
        per_contract = sizing.entry_cost / sizing.quantity
        if min(combo.gb_margin, combo.mc_margin) + combo.cost_per_contract - per_contract < cfg.min_valuation_margin:
            d.reject("NO_MODEL_CONSENSUS", "margin disappears at the final size")
            continue
        if rating.value < cfg.min_trade_rating:
            d.reject("RATING_BELOW_MINIMUM", f"{rating.value} < {cfg.min_trade_rating}")
            continue
        entry_exp, exit_dl = deadlines(spec.kickoff, cfg.hold_policy, cfg)
        est = combo.estimate
        d.plan = TradePlan(
            trade_id=trade_id, revision=1, strategy_id=cfg.strategy_id, config_version=cfg.version,
            season=spec.season, week=spec.week, game_id=spec.game_id, market_slug=spec.market_slug,
            market_type=spec.market_type, selected_outcome=spec.selected_outcome, selected_side=spec.selected_side,
            total_line=spec.total_line, settlement_rule_version=spec.settlement_rule_version,
            hold_policy=cfg.hold_policy, kickoff=spec.kickoff, decision_at=inp.now, entry_active_after=inp.now,
            entry_expires_at=entry_exp, exit_deadline=exit_dl, entry_limit=combo.entry, target_limit=combo.target,
            stop_trigger=combo.stop, quantity=sizing.quantity, tick_size=spec.tick_size,
            quantity_step=spec.quantity_step, min_quantity=spec.min_quantity or spec.quantity_step,
            estimated_entry_cost=sizing.entry_cost, planned_loss=sizing.planned_loss,
            full_funded_loss=sizing.full_funded_loss,
            meta={"plan_key": plan_key, "bucket": combo.bucket, "gb": inp.gb.to_dict(), "mc": inp.mc.to_dict(),
                  "path_estimate": est.to_dict(), "rating": rating.to_dict(),
                  "evidence": "recorded price history" if est.recorded else "modeled price path (no recorded history yet)",
                  "target_multiple": str((combo.target / combo.entry).quantize(Decimal("0.01"))),
                  "quote_as_of": iso(inp.snapshot.received_at), "ranking_objective": cfg.ranking_objective,
                  "fee_theta": str(theta)})
        d.notes += [f"(other grid points) {c}" for c in d.codes]
        d.codes = []
        d.accepted, d.rating, d.best = True, rating, combo
        d.sizing = {"quantity": str(sizing.quantity), "binding": sizing.binding,
                    "limits": {k: str(v) for k, v in sizing.limits.items()}}
        return d
    return d
