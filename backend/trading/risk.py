"""Position sizing, exposure limits and drawdown suspensions.

Definitions (brief section 10):

    B    = conservative account equity
    C(q) = all-in entry cost for q contracts (book-walked to the limit, plus fees)
    L(q) = C(q) - net exit proceeds at the configured stressed stop execution

The sizer returns the largest legal quantity satisfying every ceiling at once,
assuming every working entry could fill simultaneously. The limits are
ceilings, not allocations. A stop is a planned exit policy, not a bound on what
a position can lose, so the full funded loss C(q) is always reported too.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .common import ONE, ZERO, D, floor_step
from .config import StrategyConfig
from .fees import execution_fee
from .paper_engine import ACTIVE_ENTRY, CLOSED, PaperEngine


@dataclass(frozen=True)
class Exposure:
    committed: Decimal  # open cost basis + reserved working entry cash
    planned_loss: Decimal  # stressed-stop loss of open inventory + working entries if filled
    positions: int  # trades with open inventory or a working entry
    games: dict  # game_id -> active trade count


@dataclass(frozen=True)
class SizingResult:
    quantity: Decimal
    entry_cost: Decimal | None
    planned_loss: Decimal | None
    full_funded_loss: Decimal | None
    binding: str
    limits: dict


def entry_cost(q: Decimal, limit: Decimal, theta: Decimal) -> Decimal:
    """Conservative: every contract at the limit price, one taker execution."""
    return q * limit + execution_fee(theta, [(limit, q)])


def stressed_stop_net(q: Decimal, stop: Decimal, delta: Decimal, theta: Decimal) -> Decimal:
    price = stop - delta
    if price <= 0:
        return ZERO
    return q * price - execution_fee(theta, [(price, q)])


def planned_loss(q: Decimal, limit: Decimal, stop: Decimal, delta: Decimal, theta: Decimal) -> Decimal:
    return entry_cost(q, limit, theta) - stressed_stop_net(q, stop, delta, theta)


def exposure(engine: PaperEngine, theta_for) -> Exposure:
    s, cfg = engine.state, engine.cfg
    committed = planned = ZERO
    count, games = 0, {}
    for tid, plan in s.plans.items():
        o, pos = s.orders.get(tid), s.positions.get(tid)
        open_qty = pos.qty_open if pos and pos.status != CLOSED else ZERO
        working = (o.quantity - o.filled) if o and o.status in ACTIVE_ENTRY else ZERO
        if open_qty <= 0 and working <= 0:
            continue
        theta = theta_for(plan)
        count += 1
        games[plan.game_id] = games.get(plan.game_id, 0) + 1
        committed += (pos.cost_basis if pos else ZERO) + (o.reserved if o and o.status in ACTIVE_ENTRY else ZERO)
        if open_qty > 0:
            planned += max(ZERO, pos.cost_basis - stressed_stop_net(open_qty, plan.stop_trigger,
                                                                    cfg.stop_stress_price_delta, theta))
        if working > 0:
            planned += planned_loss(working, plan.entry_limit, plan.stop_trigger, cfg.stop_stress_price_delta, theta)
    return Exposure(committed=committed, planned_loss=planned, positions=count, games=games)


def depth_limit(asks: list, limit: Decimal, participation: Decimal, step: Decimal) -> Decimal:
    """Contracts available at or below the limit after the participation cap."""
    total = sum((q for p, q in asks if p <= limit), ZERO)
    return floor_step(total * participation, step)


def size(*, rating: Decimal, equity: Decimal, available_cash: Decimal, exposure: Exposure, game_id: str,
         entry_limit: Decimal, stop_trigger: Decimal, theta: Decimal, selected_asks: list,
         cfg: StrategyConfig, step: Decimal = ONE, min_qty: Decimal = ONE) -> SizingResult:
    B = D(equity)
    risk_fraction = cfg.risk_fraction(rating)
    limits = {
        "tier_planned_loss": risk_fraction * B,
        "funded_loss_per_game": cfg.max_funded_loss_fraction_per_game * B,
        "total_committed": cfg.max_total_committed_fraction * B - exposure.committed,
        "aggregate_planned_loss": cfg.max_aggregate_planned_loss_fraction * B - exposure.planned_loss,
        "available_cash": available_cash,
    }
    if risk_fraction <= 0:
        return SizingResult(ZERO, None, None, None, "rating below minimum", limits)
    if exposure.positions >= cfg.max_open_positions:
        return SizingResult(ZERO, None, None, None, "max concurrent positions", limits)
    if exposure.games.get(game_id, 0) >= cfg.max_active_trades_per_game:
        return SizingResult(ZERO, None, None, None, "max active trades per game", limits)
    q = depth_limit(selected_asks, entry_limit, cfg.entry_participation_cap, step)
    if q < min_qty:
        return SizingResult(ZERO, None, None, None, "insufficient depth at entry limit", limits)
    binding = "depth"
    while q >= min_qty:
        C = entry_cost(q, entry_limit, theta)
        L = planned_loss(q, entry_limit, stop_trigger, cfg.stop_stress_price_delta, theta)
        failed = None
        if L > limits["tier_planned_loss"]:
            failed = "tier_planned_loss"
        elif C > limits["funded_loss_per_game"]:
            failed = "funded_loss_per_game"
        elif C > limits["total_committed"]:
            failed = "total_committed"
        elif L > limits["aggregate_planned_loss"]:
            failed = "aggregate_planned_loss"
        elif C > limits["available_cash"]:
            failed = "available_cash"
        if failed is None:
            return SizingResult(q, C, L, C, binding, limits)
        binding = failed
        q -= step
    return SizingResult(ZERO, None, None, None, binding, limits)


def suspension(engine: PaperEngine, equity: Decimal, high_water: Decimal, season: int, week: int,
               cfg: StrategyConfig) -> str | None:
    """New-entry suspension reason, or None. Exit monitoring is never affected.

    ``high_water`` must be the high-water equity observed since the most recent
    recorded strategy review (or since inception when there has been none).
    """
    s = engine.state
    if s.paused:
        return f"operator pause: {s.pause_reason}"
    week_open = s.week_open.get(f"{season}-{week}")
    if week_open and week_open > 0 and (week_open - equity) / week_open >= cfg.weekly_new_entry_pause_drawdown:
        return "weekly drawdown limit reached"
    reference = high_water
    if s.review_reference_equity is not None:
        reference = max(high_water, s.review_reference_equity)
    if reference > 0 and (reference - equity) / reference >= cfg.account_review_drawdown:
        return "account drawdown review required"
    return None
