"""Pure Oil D decisions and simulated IOC fills for the stream paper account."""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math

from ..updown.strategy import Book, Decision, Settings, Window, evaluate
from .live_books import ENTRY_EXCHANGE_MAX_AGE, EXIT_BUFFER, SIGNAL_TTL, SUBMISSION_BUFFER, usable


ZERO = Decimal(0)
ONE = Decimal(1)
DEPTH_SHARE = Decimal("0.25")
ENTRY_CAP = Decimal("1.00")
LOSS_CAP = Decimal("2.00")
FIXED_FEE_RESERVE = Decimal("0.02")
MIN_FEE_RESERVE = Decimal("0.03")
_MONEY_METRICS = frozenset(("max_loss", "payout_if_right", "profit_if_right", "expected_profit", "open_stake_after",
                            "profit_at_take_profit", "loss_at_stop"))


@dataclass(frozen=True)
class ShadowIntent:
    slug: str
    side: str
    signal_at: float
    ready_at: float
    deadline: float
    limit: Decimal
    quantity: Decimal
    budget: Decimal
    fair_up: float
    theta: Decimal
    min_edge: Decimal
    signal_book_at: float
    signal_book_exchange_at: float
    signal_book_sequence: int | None
    signal_book_levels: tuple


@dataclass(frozen=True)
class ShadowDecision:
    reason: str
    intent: ShadowIntent | None = None
    decision: Decision | None = None


@dataclass(frozen=True)
class ShadowFill:
    side: str
    quantity: Decimal
    limit: Decimal
    average_price: Decimal
    fee: Decimal
    cost: Decimal
    levels: tuple[tuple[Decimal, Decimal], ...]
    filled_at: float


@dataclass(frozen=True)
class ShadowExecution:
    reason: str
    fill: ShadowFill | None = None
    best_ask: Decimal | None = None
    eligible_depth: Decimal | None = None
    required_depth: Decimal | None = None
    eligible_quantity: Decimal | None = None


def _amount(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Expected a finite Oil D shadow amount")
    return amount


def _floor(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _levels(book):
    return tuple(tuple(tuple(level) for level in levels) for levels in (book.yes_bids, book.yes_offers))


def _from_quantity_units(decision, step):
    scale = float(step)
    return replace(decision, quantity=float(decision.quantity * step), fee=decision.fee * scale,
                   cost=decision.cost * scale, fills=tuple((price, float(quantity * step))
                                                          for price, quantity in decision.fills),
                   risk={key: value * scale if key in _MONEY_METRICS and value is not None else value
                         for key, value in decision.risk.items()})


def signal_for_quote(window: Window, book: Book | None, *, now: float, fair_up: float | None, settings: Settings,
                     balance, open_stake, today_pnl, held=False, minimum_quantity=1) -> ShadowDecision:
    """Run the desk's decision rule on a stream quote, in integer venue-size units."""
    if window.key != "wti-15m" or window.end - window.start != 900:
        return ShadowDecision("wrong_market")
    if book is None:
        return ShadowDecision("book_unavailable")
    if not usable(book, now, exchange_max_age=ENTRY_EXCHANGE_MAX_AGE):
        return ShadowDecision("book_stale")
    if fair_up is None or not math.isfinite(fair_up) or not 0 <= fair_up <= 1:
        return ShadowDecision("fair_value_unavailable")
    try:
        step = _amount(minimum_quantity)
        if step <= ZERO:
            return ShadowDecision("invalid_contract_terms")
        balances = tuple(float(_amount(value) / step) for value in (balance, open_stake, today_pnl))
        theta = _amount(window.theta)
        edge = _amount(settings.min_edge)
    except (InvalidOperation, ValueError):
        return ShadowDecision("invalid_inputs")
    if theta < ZERO or edge < ZERO or not all(map(math.isfinite, balances)) or balances[0] <= 0 or balances[1] < 0:
        return ShadowDecision("invalid_inputs")

    # The shared decision engine sizes integer contracts. Scale depth and all cash inputs
    # together so its Kelly/fee logic can evaluate integer multiples of a venue lot.
    def in_quantity_units(levels):
        return tuple((price, float(_amount(size) / step)) for price, size in levels)

    scaled = replace(book, yes_bids=in_quantity_units(book.yes_bids), yes_offers=in_quantity_units(book.yes_offers))
    signal_settings = replace(settings, participation=float(DEPTH_SHARE))
    decision = evaluate(window, scaled, now, 0.0, 0.0, None, None, *balances, held, signal_settings,
                        p_up=fair_up, cross_check=False)
    normalized = _from_quantity_units(decision, step)
    if decision.action != "BUY":
        return ShadowDecision(decision.reason, decision=normalized)

    ready_at = now + settings.execution_latency_seconds
    deadline = min(ready_at + SIGNAL_TTL, window.end - max(EXIT_BUFFER, settings.entry_before_end_seconds)
                   - SUBMISSION_BUFFER)
    if ready_at >= deadline:
        return ShadowDecision("signal_expires_before_ready", decision=normalized)
    intent = ShadowIntent(window.slug, decision.side, now, ready_at, deadline, _amount(decision.fills[-1][0]),
                          decision.quantity * step, _amount(decision.cost) * step, fair_up, theta, edge, book.at,
                          book.exchange_at, book.sequence, _levels(book))
    return ShadowDecision("buy_signal", intent, normalized)


def _book_arrival_reason(intent, book, now):
    if book is None:
        return "book_unavailable"
    if not usable(book, now, exchange_max_age=ENTRY_EXCHANGE_MAX_AGE):
        return "book_stale"
    if book.at < intent.ready_at:
        return "frame_before_ready"
    if book.exchange_at >= intent.deadline:
        return "frame_after_deadline"
    if (book.exchange_at < intent.signal_book_exchange_at or
            (book.sequence is not None and intent.signal_book_sequence is not None
             and book.sequence < intent.signal_book_sequence)):
        return "book_out_of_order"
    if (book.at <= intent.signal_book_at or
            (book.exchange_at == intent.signal_book_exchange_at and book.sequence == intent.signal_book_sequence
             and _levels(book) == intent.signal_book_levels)):
        return "book_not_advanced"
    return None


def simulate_arrival(intent: ShadowIntent, book: Book | None, *, now: float, minimum_quantity, price_tick,
                     buying_power, pilot_budget=12, net_pnl=0, market_open=True,
                     entry_cap=ENTRY_CAP, loss_cap=LOSS_CAP) -> ShadowExecution:
    """Size a fresh-book fill at the original side and limit with the account's caps."""
    if now >= intent.deadline:
        return ShadowExecution("signal_expired")
    if not market_open:
        return ShadowExecution("market_not_open")
    if now < intent.ready_at:
        return ShadowExecution("waiting_for_latency")
    reason = _book_arrival_reason(intent, book, now)
    if reason is not None:
        return ShadowExecution(reason)

    try:
        step, tick = _amount(minimum_quantity), _amount(price_tick)
        funds, pilot, realized = _amount(buying_power), _amount(pilot_budget), _amount(net_pnl)
        entry_bound, loss_bound = _amount(entry_cap), _amount(loss_cap)
    except (InvalidOperation, ValueError):
        return ShadowExecution("invalid_contract_terms_or_budget")
    if entry_bound <= ZERO or loss_bound <= ZERO:
        return ShadowExecution("invalid_contract_terms_or_budget")
    if step <= ZERO or not ZERO < tick < ONE:
        return ShadowExecution("invalid_contract_terms")
    limit = _floor(intent.limit, tick)
    if not (tick <= limit <= ONE - tick and Decimal("0.01") <= limit <= Decimal("0.99")):
        return ShadowExecution("invalid_limit")
    if realized <= -loss_bound:
        return ShadowExecution("loss_cap_reached")

    asks = tuple((_amount(price), _amount(size)) for price, size in book.side_asks(intent.side))
    best_ask = asks[0][0] if asks else None
    required_depth = step / DEPTH_SHARE
    diagnostics = {"best_ask": best_ask, "required_depth": required_depth}
    if best_ask is not None and best_ask > limit:
        return ShadowExecution("price_above_limit", eligible_depth=ZERO, eligible_quantity=ZERO, **diagnostics)

    fair = _amount(intent.fair_up)
    if intent.side == "DOWN":
        fair = ONE - fair
    eligible = []
    for price, size in asks:
        if price > limit:
            break
        modeled_fee = intent.theta * price * (ONE - price)
        if fair - price - modeled_fee + Decimal("0.0000000001") < intent.min_edge:
            break
        eligible.append((price, size, modeled_fee))
    depth = sum((size for _, size, _ in eligible), ZERO)
    budget = min(entry_bound, pilot, funds, loss_bound + min(realized, ZERO), intent.budget)
    reserve_per_contract = max(MIN_FEE_RESERVE, intent.theta / 4)
    ceiling = _floor(min(intent.quantity, max(ZERO, budget - FIXED_FEE_RESERVE) / (limit + reserve_per_contract)), step)
    if ceiling < step:
        return ShadowExecution("budget_below_minimum", eligible_depth=depth, eligible_quantity=ZERO, **diagnostics)
    if depth < required_depth:
        return ShadowExecution("insufficient_depth", eligible_depth=depth, eligible_quantity=ZERO, **diagnostics)

    # Each price level contributes up to its own available share, once and in price order.
    remaining, gross, fee, levels = ceiling, ZERO, ZERO, []
    for price, size, modeled_fee in eligible:
        quantity = min(remaining, _floor(DEPTH_SHARE * size, step))
        if quantity <= ZERO:
            continue
        levels.append((price, quantity))
        gross += price * quantity
        fee += modeled_fee * quantity
        remaining -= quantity
        if not remaining:
            break
    filled = ceiling - remaining
    if filled < step:
        return ShadowExecution("insufficient_depth", eligible_depth=depth, eligible_quantity=filled, **diagnostics)
    fill = ShadowFill(intent.side, filled, limit, gross / filled, fee, gross + fee, tuple(levels), now)
    return ShadowExecution("filled", fill, eligible_depth=depth, eligible_quantity=filled, **diagnostics)
