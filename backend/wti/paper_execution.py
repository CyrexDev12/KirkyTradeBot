"""Match Oil D paper orders against observed books and model the protective exit lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP
from math import isfinite

from ..updown.strategy import Book
from .live_books import BOOK_MAX_AGE, ENTRY_EXCHANGE_MAX_AGE, EXCHANGE_MAX_AGE, EXIT_BUFFER, usable


ZERO, ONE = Decimal(0), Decimal(1)
CENT = Decimal(".01")
TARGET_DEPTH_SHARE = Decimal(".25")
STOP_RETRY_SECONDS = 2.0
EXIT_SEND_BUFFER = 1.0
DECISION_INTERVAL = 1.0
MAX_UNOBSERVED_SECONDS = 20.0


def _amount(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Expected a finite paper order amount")
    return amount


def _aligned(value, step, rounding=ROUND_DOWN):
    return (value / step).to_integral_value(rounding=rounding) * step


def _share(value):
    share = _amount(value)
    if not ZERO < share <= ONE:
        raise ValueError("Paper depth share must be in (0, 1]")
    return share


def modeled_fee(levels, coefficient):
    """Estimate commission from actual matched price levels, rounded once per order."""
    theta = _amount(coefficient)
    return sum((qty * theta * price * (ONE - price) for price, qty in levels), ZERO).quantize(
        CENT, rounding=ROUND_HALF_EVEN
    )


def _native_levels(rows):
    amounts = {}
    for price, size in rows:
        price, size = _amount(price), _amount(size)
        if not ZERO < price < ONE or size < ZERO:
            raise ValueError("Invalid displayed book level")
        if size:
            amounts[price] = amounts.get(price, ZERO) + size
    return amounts


@dataclass(frozen=True)
class MatchResult:
    outcome: str
    reason: str
    requested_quantity: Decimal
    arrival_at: float
    quote_at: float | None = None
    exchange_at: float | None = None
    filled_quantity: Decimal = ZERO
    average_price: Decimal | None = None
    gross: Decimal = ZERO
    fee: Decimal = ZERO
    levels: tuple[tuple[Decimal, Decimal], ...] = ()

    @property
    def quantity(self):
        return self.filled_quantity


class PaperExecution:
    """Track consumed shares of a single market's native bids and offers.

    Oil D applies its 25% depth check when sizing an entry. Matching an
    already sized IOC uses the remaining displayed book by default. A separate
    configurable share allows depth sensitivity tests.
    """

    def __init__(self, *, depth_share=ONE, state=None):
        self.depth_share = _share(depth_share)
        state = state or {}
        self._stamp = tuple(state["stamp"]) if state.get("stamp") is not None else None
        self._levels = {side: _native_levels(state.get("levels", {}).get(side, ())) for side in ("bid", "offer")}
        self._spent = {side: _native_levels(state.get("spent", {}).get(side, ())) for side in ("bid", "offer")}

    def dump(self):
        def encode(books):
            return {side: [[str(price), str(quantity)] for price, quantity in levels.items()]
                    for side, levels in books.items()}

        return {"stamp": list(self._stamp) if self._stamp is not None else None,
                "levels": encode(self._levels), "spent": encode(self._spent)}

    def _observe(self, book):
        levels = {"bid": _native_levels(book.yes_bids), "offer": _native_levels(book.yes_offers)}
        at, exchange_at, sequence = book.at, book.exchange_at, book.sequence
        if self._stamp is not None:
            old_at, old_exchange, old_sequence = self._stamp
            if (at < old_at or exchange_at < old_exchange
                    or (sequence is not None and old_sequence is not None and sequence < old_sequence)):
                return "book_out_of_order"
            if exchange_at == old_exchange and (sequence is None or old_sequence is None or sequence == old_sequence):
                if levels != self._levels:
                    return "conflicting_book_frame"
                # A repeated event cannot refresh receipt age or supply new liquidity.
                return "repeated_book_frame" if at > old_at else None
        self._stamp, self._levels = (at, exchange_at, sequence), levels
        for side, spent in self._spent.items():
            self._spent[side] = {price: min(size, spent[price]) for price, size in levels[side].items() if price in spent}
        return None

    def _best_bid(self, side):
        if side == "UP":
            return max(self._levels["bid"], default=None)
        best_offer = min(self._levels["offer"], default=None)
        return ONE - best_offer if best_offer is not None else None

    def match_ioc(self, book: Book | None, *, side, action, quantity, limit, arrival_at, cutoff, quantity_step,
                  fee_coefficient, market_open=True, exchange_max_age=None, minimum_quote_at=None) -> MatchResult:
        if exchange_max_age is None:
            exchange_max_age = ENTRY_EXCHANGE_MAX_AGE if action == "buy" else EXCHANGE_MAX_AGE
        return self._match(book, side, action, quantity, limit, arrival_at, cutoff, quantity_step, fee_coefficient,
                           market_open, exchange_max_age, minimum_quote_at, self.depth_share, target=False)

    def match_target(self, book: Book | None, *, side, quantity, limit, arrival_at, cutoff, quantity_step,
                     fee_coefficient, market_open=True, activated_at=None,
                     depth_share=TARGET_DEPTH_SHARE) -> MatchResult:
        """Credit a resting-order proxy only when bids at or above the target were observed."""
        return self._match(book, side, "sell", quantity, limit, arrival_at, cutoff, quantity_step, fee_coefficient,
                           market_open, EXCHANGE_MAX_AGE, activated_at, _share(depth_share), target=True)

    def _match(self, book, side, action, quantity, limit, arrival_at, cutoff, quantity_step, fee_coefficient,
               market_open, exchange_max_age, minimum_quote_at, share, *, target):
        size, price, step, theta = map(_amount, (quantity, limit, quantity_step, fee_coefficient))
        if (side not in ("UP", "DOWN") or action not in ("buy", "sell") or not ZERO < price < ONE
                or step <= ZERO or size < step or size != _aligned(size, step) or theta < ZERO
                or not isfinite(arrival_at) or not isfinite(cutoff) or not isfinite(exchange_max_age)
                or exchange_max_age < 0 or (minimum_quote_at is not None and not isfinite(minimum_quote_at))):
            raise ValueError("Invalid simulated order terms")

        quote_at = book.at if book is not None else None
        exchange_at = book.exchange_at if book is not None else None
        base = MatchResult("unverified", "book_unavailable", size, arrival_at, quote_at, exchange_at)
        if arrival_at >= cutoff:
            return replace(base, outcome="expired", reason="order_after_cutoff")
        if not market_open:
            return replace(base, outcome="no_fill", reason="market_not_open")
        if book is None:
            return base
        if exchange_at is not None and exchange_at >= cutoff:
            return replace(base, reason="book_after_cutoff")
        if not usable(book, arrival_at, exchange_max_age=exchange_max_age):
            return replace(base, reason="book_stale_or_future")
        if minimum_quote_at is not None and book.at < minimum_quote_at:
            return replace(base, reason="book_before_ready")
        try:
            stale = self._observe(book)
        except (InvalidOperation, ValueError):
            return replace(base, reason="invalid_book_levels")
        if stale:
            return replace(base, reason=stale)

        buying = action == "buy"
        # Buying DOWN consumes the native YES bids used by a sell of UP.
        native_side = "offer" if buying == (side == "UP") else "bid"
        side_price = (lambda value: value) if side == "UP" else (lambda value: ONE - value)
        levels = sorted(self._levels[native_side].items(), key=lambda row: side_price(row[0]), reverse=not buying)
        spent, remaining, matched = self._spent[native_side], size, []
        for native_price, displayed in levels:
            level_price = side_price(native_price)
            if (buying and level_price > price) or (not buying and level_price < price):
                break
            available = max(ZERO, share * displayed - spent.get(native_price, ZERO))
            take = min(remaining, _aligned(available, step))
            if not take:
                continue
            spent[native_price] = spent.get(native_price, ZERO) + take
            matched.append((price if target else level_price, take))
            remaining -= take
            if not remaining:
                break
        filled = size - remaining
        if not filled:
            return replace(base, outcome="no_fill", reason="no_available_depth_at_limit")
        gross = sum((price * take for price, take in matched), ZERO)
        return replace(base, outcome="partial" if remaining else "filled",
                       reason="displayed_target_proxy" if target else "displayed_ioc_depth", filled_quantity=filled,
                       average_price=gross / filled, gross=gross, fee=modeled_fee(matched, theta),
                       levels=tuple(matched))


@dataclass(frozen=True)
class ExitEvent:
    kind: str
    at: float
    match: MatchResult | None = None
    quantity: Decimal = ZERO
    proceeds: Decimal = ZERO
    limit: Decimal | None = None


class PaperExits:
    """Manage a simulated resting target and stop IOC for a filled position."""

    def __init__(self, execution: PaperExecution, *, side, quantity, entry_price, entry_at, window_end, quantity_step,
                 price_tick, fee_coefficient, take_profit_fraction=.5, stop_loss_fraction=.5, target_latency=.25,
                 cancel_latency=.25, stop_latency=.25, target_depth_share=TARGET_DEPTH_SHARE,
                 decision_interval=DECISION_INTERVAL, state=None):
        step, tick, quantity, price, theta = map(_amount, (quantity_step, price_tick, quantity, entry_price,
                                                          fee_coefficient))
        profit, loss = map(_amount, (take_profit_fraction, stop_loss_fraction))
        if (side not in ("UP", "DOWN") or step <= ZERO or not ZERO < tick <= Decimal(".5")
                or quantity < step or quantity != _aligned(quantity, step) or not ZERO < price < ONE or theta < ZERO
                or not ZERO < profit < ONE or not ZERO < loss < ONE or not isfinite(entry_at)
                or not isfinite(window_end) or entry_at >= window_end
                or any(not isfinite(delay) or delay < 0
                       for delay in (target_latency, cancel_latency, stop_latency, decision_interval))):
            raise ValueError("Invalid simulated position or exit terms")
        self.execution, self.side = execution, side
        self.step, self.tick, self.theta = step, tick, theta
        self.window_end, self.cutoff = window_end, window_end - EXIT_BUFFER
        self.target = min(ONE - tick, _aligned(price + profit * (ONE - price), tick, ROUND_UP))
        self.stop = max(tick, _aligned(price * (ONE - loss), tick))
        self.target_latency, self.cancel_latency, self.stop_latency = target_latency, cancel_latency, stop_latency
        self.decision_interval = decision_interval
        self.target_depth_share = _share(target_depth_share)
        state = state or {}
        self.state = state.get("state", "awaiting_target")
        self.remaining = _amount(state.get("remaining", quantity))
        self.gross = _amount(state.get("gross", ZERO))
        self.fees = _amount(state.get("fees", ZERO))
        self.settled_proceeds = _amount(state.get("settled_proceeds", ZERO))
        self.stop_at = state.get("stop_at")
        self.coverage_reason = state.get("coverage_reason")
        self._last_at = state.get("last_at", entry_at)
        self._decision_at = state.get("decision_at", entry_at)
        self._covered_until = state.get("covered_until", entry_at)
        self._action_at = state.get("action_at")
        self._target_active_at = state.get("target_active_at")
        self._retry_at = state.get("retry_at", entry_at)
        stop_limit = state.get("stop_limit")
        self._stop_limit = _amount(stop_limit) if stop_limit is not None else None

    @property
    def unverified(self):
        return self.coverage_reason is not None

    @property
    def next_action_at(self):
        if not self.remaining:
            return None
        if self.state == "awaiting_settlement":
            return self.window_end if self._last_at < self.window_end else None
        if self._action_at is not None:
            return min(self.cutoff, self._action_at) if self._last_at < self.cutoff else self._action_at
        next_check = max(self._decision_at, self._retry_at) if self.state == "stop_waiting" else self._decision_at
        return min(self.cutoff, max(self._last_at, next_check)) if self._last_at < self.cutoff else None

    def dump(self):
        return {
            "state": self.state, "remaining": str(self.remaining), "gross": str(self.gross), "fees": str(self.fees),
            "settled_proceeds": str(self.settled_proceeds), "stop_at": self.stop_at,
            "coverage_reason": self.coverage_reason, "last_at": self._last_at, "covered_until": self._covered_until,
            "decision_at": self._decision_at,
            "action_at": self._action_at, "target_active_at": self._target_active_at, "retry_at": self._retry_at,
            "stop_limit": str(self._stop_limit) if self._stop_limit is not None else None,
        }

    def _observe(self, at, book, market_open):
        active_at = min(at, self.cutoff)
        if active_at - self._covered_until > MAX_UNOBSERVED_SECONDS:
            self.coverage_reason = self.coverage_reason or "extended_quote_gap"
        if market_open and book is not None and usable(book, at) and book.exchange_at < self.cutoff:
            try:
                if self.execution._observe(book) is None:
                    self._covered_until = min(self.cutoff, book.at + BOOK_MAX_AGE, book.exchange_at + EXCHANGE_MAX_AGE)
                    return book
            except (InvalidOperation, ValueError):
                pass
        self._covered_until = min(self._covered_until, active_at) if market_open else active_at
        return None

    def _apply(self, at, match, kind, events):
        if match.quantity:
            self.remaining -= match.quantity
            self.gross += match.gross
            self.fees += match.fee
        if kind == "stop_ioc" or match.quantity:
            limit = self._stop_limit if kind == "stop_ioc" else self.target
            events.append(ExitEvent(kind, at, match, match.quantity, match.gross, limit))
        if not self.remaining:
            self.state = "closed"
            self._action_at = None

    def _cancel_target(self, at, events):
        if self.state == "target_working":
            self.state, self._action_at = "target_cancelling", at + self.cancel_latency
            events.append(ExitEvent("target_cancel_requested", at, quantity=self.remaining, limit=self.target))

    def _activate_or_cancel(self, at, events):
        if self._action_at is None or at < self._action_at:
            return
        if self.state == "target_pending":
            self.state, self._target_active_at, self._action_at = "target_working", self._action_at, None
            events.append(ExitEvent("target_working", at, quantity=self.remaining, limit=self.target))
        elif self.state == "target_cancelling":
            self.state = "stop_waiting" if self.stop_at is not None else "awaiting_target"
            self._action_at = self._target_active_at = None
            events.append(ExitEvent("target_cancelled", at, quantity=self.remaining, limit=self.target))

    def _settle(self, at, official_yes_payout, events):
        if at < self.window_end or self.state != "awaiting_settlement" or official_yes_payout is None:
            return
        yes = _amount(official_yes_payout)
        if not ZERO <= yes <= ONE:
            raise ValueError("Expected an official YES settlement payout in [0, 1]")
        proceeds = self.remaining * (yes if self.side == "UP" else ONE - yes)
        events.append(ExitEvent("settled", at, quantity=self.remaining, proceeds=proceeds))
        self.state, self._action_at = "closed", None
        self.settled_proceeds += proceeds
        self.remaining = ZERO

    def advance(self, at, book: Book | None, *, market_open=True, official_yes_payout=None) -> tuple[ExitEvent, ...]:
        """Apply current market evidence and actions due by the current simulation time."""
        if not isfinite(at) or at < self._last_at:
            raise ValueError("Paper exit time went backwards")
        if not self.remaining:
            return ()
        self._last_at = at
        events = []
        book = self._observe(at, book, market_open)

        if at >= self.cutoff and self.state == "target_pending" and self._action_at >= self.cutoff:
            self.state, self._action_at = "awaiting_settlement", None
            events.append(ExitEvent("target_expired", at, quantity=self.remaining, limit=self.target))
        self._activate_or_cancel(at, events)
        if at >= self.cutoff or not market_open:
            self._cancel_target(at, events)
            if self.cancel_latency == 0:
                self._activate_or_cancel(at, events)
        if self.state == "stop_pending" and at >= self._action_at:
            match = self.execution.match_ioc(
                book, side=self.side, action="sell", quantity=self.remaining, limit=self._stop_limit, arrival_at=at,
                cutoff=self.cutoff, quantity_step=self.step, fee_coefficient=self.theta, market_open=market_open,
            )
            self.state, self._action_at = "stop_waiting", None
            if match.outcome in ("unverified", "expired"):
                self.coverage_reason = self.coverage_reason or match.reason
            self._apply(at, match, "stop_ioc", events)
        if at >= self.cutoff:
            if self.remaining and self.state not in ("target_cancelling", "stop_pending"):
                self.state = "awaiting_settlement"
            self._settle(at, official_yes_payout, events)
            return tuple(events)
        if not self.remaining:
            return tuple(events)
        may_decide = (at >= self._decision_at and self.state in ("awaiting_target", "target_working", "stop_waiting"))
        if may_decide:
            self._decision_at = at + self.decision_interval
        if book is None:
            return tuple(events)

        best_bid = self.execution._best_bid(self.side)
        if may_decide and self.stop_at is None and best_bid is not None and best_bid <= self.stop:
            self.stop_at = at
            events.append(ExitEvent("stop_triggered", at, quantity=self.remaining, limit=self.stop))
            self._cancel_target(at, events)
            if self.state == "awaiting_target":
                self.state = "stop_waiting"
            if self.cancel_latency == 0:
                self._activate_or_cancel(at, events)

        if may_decide and self.state == "awaiting_target" and at + EXIT_SEND_BUFFER < self.cutoff:
            self.state, self._action_at = "target_pending", at + self.target_latency
            events.append(ExitEvent("target_submitted", at, quantity=self.remaining, limit=self.target))
            if self.target_latency == 0:
                self._activate_or_cancel(at, events)

        if self.state in ("target_working", "target_cancelling") and self._target_active_at is not None:
            match = self.execution.match_target(
                book, side=self.side, quantity=self.remaining, limit=self.target, arrival_at=at,
                cutoff=self.cutoff, quantity_step=self.step, fee_coefficient=self.theta,
                activated_at=self._target_active_at, depth_share=self.target_depth_share,
            )
            self._apply(at, match, "target_fill", events)

        if (may_decide and self.state == "stop_waiting" and best_bid is not None and at >= self._retry_at
                and at + max(EXIT_SEND_BUFFER, self.stop_latency) < self.cutoff):
            self._stop_limit = max(self.tick, _aligned(best_bid - 2 * self.tick, self.tick))
            self.state, self._action_at, self._retry_at = "stop_pending", at + self.stop_latency, at + STOP_RETRY_SECONDS
            events.append(ExitEvent("stop_submitted", at, quantity=self.remaining, limit=self._stop_limit))
            if self.stop_latency == 0:
                events.extend(self.advance(at, book, market_open=market_open))
        return tuple(events)
