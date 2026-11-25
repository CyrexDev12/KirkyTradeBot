"""Replay hypothetical Oil D exit rules over a fixed set of confirmed positions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP
from math import inf, isfinite

from .live_books import BOOK_MAX_AGE, EXCHANGE_MAX_AGE, EXIT_BUFFER, usable
from .live_exit_policy_books import BookDepth, DEFAULT_DEPTH_SHARE


ZERO, ONE = Decimal(0), Decimal(1)
CENT = Decimal(".01")
STOP_RETRY_SECONDS = 2.0
EXIT_SEND_BUFFER = 1.0
MAX_UNOBSERVED_SECONDS = 20.0


@dataclass(frozen=True)
class ReplayPosition:
    side: str
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    entry_at: float
    window_end: float
    price_tick: Decimal
    minimum_quantity: Decimal
    fee_coefficient: Decimal
    yes_payout: Decimal | None
    target_at: float | None = None
    target_limit: Decimal | None = None


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    stop_confirmation_seconds: float | None


@dataclass(frozen=True)
class PolicyFill:
    kind: str
    at: float
    quantity: Decimal
    average_price: Decimal
    limit: Decimal
    gross: Decimal
    modeled_fee: Decimal


@dataclass(frozen=True)
class PolicyReplay:
    status: str
    reason: str | None = None
    net_pnl: Decimal | None = None
    target_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_quantity: Decimal = ZERO
    stop_quantity: Decimal = ZERO
    settled_quantity: Decimal | None = None
    target_gross: Decimal = ZERO
    stop_gross: Decimal = ZERO
    exit_fees: Decimal = ZERO
    settlement_proceeds: Decimal | None = None
    stop_trigger_at: float | None = None
    fresh_updates: int = 0
    gap_events: int = 0
    longest_unobserved_seconds: float = 0.0
    fills: tuple[PolicyFill, ...] = ()


@dataclass
class _BidCoverage:
    position_starts_at: float
    missing_since: float | None
    longest: float = 0.0

    def missing(self, at):
        if self.missing_since is None:
            self.missing_since = max(self.position_starts_at, at)

    def clear(self, at):
        if self.missing_since is not None:
            self.longest = max(self.longest, at - self.missing_since)
            self.missing_since = None


def _amount(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _round_to(value, tick, rounding):
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _book_identity(book):
    return (book.exchange_at, book.sequence, tuple(map(tuple, book.yes_bids)), tuple(map(tuple, book.yes_offers)))


def _fee(levels, theta):
    total = sum((quantity * theta * price * (ONE - price) for price, quantity in levels), ZERO)
    return total.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _decision_prices(position, take_profit_fraction, stop_loss_fraction):
    tick, price = position.price_tick, position.entry_price
    target = position.target_limit
    if target is None:
        target = min(ONE - tick, _round_to(price + take_profit_fraction * (ONE - price), tick, ROUND_UP))
    stop = max(tick, _round_to(price * (ONE - stop_loss_fraction), tick, ROUND_DOWN))
    return target, stop


def _record_fill(fills, depth, quote, position, remaining, limit, kind, at):
    step = position.minimum_quantity
    if remaining < step or _round_to(remaining, step, ROUND_DOWN) != remaining:
        return remaining
    fill = depth.take(quote, position.side, remaining, limit)
    if fill is None:
        return remaining
    if kind == "target":
        # Executable depth supplies evidence for a fill, valued at the resting limit.
        price, levels = limit, ((limit, fill.quantity),)
    else:
        price, levels = fill.average_price, fill.levels
    gross = sum((price * quantity for price, quantity in levels), ZERO)
    fills.append(PolicyFill(kind, at, fill.quantity, price, limit, gross, _fee(levels, position.fee_coefficient)))
    return remaining - fill.quantity


def _summarize(position, target, stop, stop_at, quotes, gaps, longest_gap, fills, remaining):
    details = {"target_price": target, "stop_price": stop, "stop_trigger_at": stop_at,
               "fresh_updates": len(quotes), "gap_events": gaps, "longest_unobserved_seconds": longest_gap,
               "fills": tuple(fills)}
    if not quotes:
        return PolicyReplay("unverified", "no_usable_bid_frames", **details)
    targets = [fill for fill in fills if fill.kind == "target"]
    stops = [fill for fill in fills if fill.kind == "stop_loss"]
    target_gross = sum((fill.gross for fill in targets), ZERO)
    stop_gross = sum((fill.gross for fill in stops), ZERO)
    fees = sum((fill.modeled_fee for fill in fills), ZERO)
    details.update(target_quantity=sum((fill.quantity for fill in targets), ZERO),
                   stop_quantity=sum((fill.quantity for fill in stops), ZERO),
                   target_gross=target_gross, stop_gross=stop_gross, exit_fees=fees)
    if longest_gap > MAX_UNOBSERVED_SECONDS:
        return PolicyReplay("unverified", "extended_unobserved_quotes", **details)
    if remaining and position.yes_payout is None:
        return PolicyReplay("unverified", "official_settlement_unavailable", **details)
    payout = position.yes_payout
    proceeds = remaining * (payout if position.side == "UP" else ONE - payout) if remaining else ZERO
    net = (target_gross + stop_gross + proceeds - fees - position.entry_price * position.quantity
           - position.entry_fee)
    return PolicyReplay("modeled", net_pnl=net, settled_quantity=remaining, settlement_proceeds=proceeds, **details)


def replay_exit_policy(position, timeline, policy, *, take_profit_fraction=Decimal(".5"),
                       stop_loss_fraction=Decimal(".5"), latency=.25, depth_share=DEFAULT_DEPTH_SHARE):
    """Use book states known at each probe and a marketable-bid proxy for the resting target."""
    cutoff = position.window_end - EXIT_BUFFER
    target, stop = _decision_prices(position, take_profit_fraction, stop_loss_fraction)
    if policy.stop_confirmation_seconds is None:
        stop = None
    target_ready_at = (max(position.entry_at, position.target_at) if position.target_at is not None
                       else position.entry_at + latency)
    if target_ready_at >= cutoff:
        target_ready_at = inf

    depth = BookDepth(depth_share=depth_share, quantity_step=position.minimum_quantity)
    rows = iter(timeline)
    frame = next(rows, None)
    remaining, fills = position.quantity, []
    current = stop_pending = None
    halted = False
    coverage = _BidCoverage(position.entry_at, position.entry_at)
    below_since = below_identity = stop_at = None
    below_count, gaps = 0, 0
    quotes = set()
    at = -inf
    retry_at = -inf
    entry_wake, target_wake = position.entry_at, target_ready_at

    while remaining:
        retry_wake = retry_at if stop_at is not None and stop_pending is None and at < retry_at else inf
        at = min(frame[0] if frame is not None else inf, entry_wake, target_wake,
                 stop_pending[0] if stop_pending is not None else inf, retry_wake)
        if at >= cutoff:
            break
        if at == entry_wake:
            entry_wake = inf
        if at == target_wake:
            target_wake = inf
        if current is not None and not usable(current, at):
            coverage.missing(min(current.at + BOOK_MAX_AGE, current.exchange_at + EXCHANGE_MAX_AGE))
            current = None
            below_since = below_identity = None
            below_count = 0

        while frame is not None and frame[0] == at:
            _, quote, halted = frame
            frame = next(rows, None)
            if (not halted and quote is not None and usable(quote, at) and quote.exchange_at < position.window_end
                    and depth.observe(quote)):
                current = quote
            else:
                current = None
                below_since = below_identity = None
                below_count = 0
                if at >= position.entry_at:
                    gaps += 1

        if at < position.entry_at:
            continue
        bids = current.side_bids(position.side) if current is not None else ()
        bid = _amount(bids[0][0]) if bids else None
        if bid is not None or halted:
            coverage.clear(at)
        else:
            coverage.missing(at)
        if bid is not None:
            identity = _book_identity(current)
            quotes.add(identity)

        if stop is not None and stop_at is None:
            if bid is None or bid > stop:
                below_since = below_identity = None
                below_count = 0
            elif policy.stop_confirmation_seconds == 0:
                stop_at = at
            elif identity != below_identity:
                below_since = at if below_since is None else below_since
                below_identity = identity
                below_count += 1
                if below_count >= 2 and at - below_since >= policy.stop_confirmation_seconds:
                    stop_at = at

        if stop_at is None and bid is not None and at >= target_ready_at and bid >= target:
            remaining = _record_fill(fills, depth, current, position, remaining, target, "target", at)
        if stop_at is None:
            continue
        if (stop_pending is None and bid is not None and at >= retry_at and at + EXIT_SEND_BUFFER < cutoff):
            stop_pending = (at + latency, max(position.price_tick, bid - 2 * position.price_tick))
            retry_at = at + STOP_RETRY_SECONDS
        if stop_pending is not None and at >= stop_pending[0]:
            _, floor = stop_pending
            stop_pending = None
            if bid is not None and bid >= floor:
                remaining = _record_fill(fills, depth, current, position, remaining, floor, "stop_loss", at)

    if remaining:
        if current is not None and not halted:
            covered_to = min(cutoff, current.at + BOOK_MAX_AGE, current.exchange_at + EXCHANGE_MAX_AGE)
            coverage.missing(covered_to)
        coverage.clear(cutoff)
    return _summarize(position, target, stop, stop_at, quotes, gaps, coverage.longest, fills, remaining)


def replay_policies(position, timeline, *, take_profit_fraction=Decimal(".5"), stop_loss_fraction=Decimal(".5"),
                    latency=.25, depth_share=DEFAULT_DEPTH_SHARE, confirm_seconds=3.0):
    """Compare the same target, quantity, costs, quote stream, and deadline in every scenario."""
    target_fraction, stop_fraction, share = map(_amount, (take_profit_fraction, stop_loss_fraction, depth_share))
    if (not isfinite(latency) or latency < 0 or not isfinite(confirm_seconds) or confirm_seconds <= 0
            or not ZERO < share <= ONE or not ZERO < target_fraction < ONE or not ZERO < stop_fraction < ONE):
        raise ValueError("Exit replay delays, depth share, or bracket fractions are out of range")
    policies = (ExitPolicy("immediate_stop", 0.0), ExitPolicy("confirmed_stop", confirm_seconds),
                ExitPolicy("target_then_settlement", None))
    values = (position.quantity, position.entry_price, position.entry_fee, position.price_tick,
              position.minimum_quantity, position.fee_coefficient)
    if (not all(isinstance(value, Decimal) and value.is_finite() for value in values)
            or position.side not in ("UP", "DOWN") or not isfinite(position.entry_at)
            or not isfinite(position.window_end) or position.entry_at >= position.window_end - EXIT_BUFFER
            or (position.target_at is not None and not isfinite(position.target_at))
            or not ZERO < position.entry_price < ONE or position.quantity <= ZERO or position.entry_fee < ZERO
            or not ZERO < position.price_tick < ONE or position.minimum_quantity <= ZERO
            or position.fee_coefficient < ZERO
            or (position.target_limit is not None and not position.price_tick <= position.target_limit <= ONE - position.price_tick)
            or (position.yes_payout is not None and not ZERO <= position.yes_payout <= ONE)):
        return {policy.name: PolicyReplay("unverified", "invalid_or_missing_position_input") for policy in policies}

    frames = tuple(timeline)
    return {policy.name: replay_exit_policy(position, frames, policy, take_profit_fraction=target_fraction,
                                             stop_loss_fraction=stop_fraction, latency=latency, depth_share=share)
            for policy in policies}
