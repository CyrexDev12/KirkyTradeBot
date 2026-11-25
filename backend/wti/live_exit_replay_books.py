"""Benchmark recorded WTI exit orders against immediately available WebSocket bids."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
import json
from math import isfinite
from pathlib import Path
import time

from .live_books import EXCHANGE_MAX_AGE, usable
from .live_timing import as_decimal, readonly
from .replay_capture import StreamBookSource
from .shadow import decode_window, recorded_book


ZERO = Decimal(0)
DEFAULT_DEPTH_SHARE = Decimal("0.25")


@dataclass(frozen=True)
class ExitLeg:
    slug: str
    window_start: int
    window_end: int
    sent_at: float
    side: str
    quantity: Decimal
    limit: Decimal
    fee_coefficient: Decimal | None = None


@dataclass(frozen=True)
class ExitQuote:
    reason: str
    at: float
    book_at: float | None = None
    book_exchange_at: float | None = None
    best_bid: Decimal | None = None
    total_visible_depth: Decimal | None = None
    visible_depth: Decimal | None = None
    executable_quantity: Decimal | None = None
    average_price: Decimal | None = None
    gross_proceeds: Decimal | None = None
    modeled_fee: Decimal | None = None
    net_proceeds: Decimal | None = None
    levels: tuple[tuple[Decimal, Decimal], ...] = ()


def _stream_frames(db, window, last_at):
    rows = db.execute("""SELECT at,kind,payload FROM tape WHERE slug=? AND source='stream'
                         AND at BETWEEN ? AND ? ORDER BY at,id""", (window.slug, window.start, last_at))
    for at, kind, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        yield (at, kind, payload) if isinstance(payload, dict) else (at, "gap", {})


class _BookReplay:
    def __init__(self, window, frames):
        self.slug, self.clock_at = window.slug, window.start
        self.source = StreamBookSource(clock=lambda: self.clock_at)
        self.generation = self.source.begin(window)
        self.source.set_connected(self.generation, True)
        self.frames = iter(frames)
        self.following = next(self.frames, None)
        self.current = None
        self.reason = "no_stream_book"
        self.spent = {"UP": {}, "DOWN": {}}
        self.quantity_step = as_decimal(window.minimum_quantity)
        if self.quantity_step is not None and self.quantity_step <= 0:
            self.quantity_step = None

    @staticmethod
    def levels_for(book, side):
        return tuple((Decimal(str(price)), Decimal(str(size))) for price, size in book.side_bids(side))

    def _sync_depth(self, book):
        if book is None or book == self.current:
            return
        for side, spent in self.spent.items():
            # Replenishment is limited to observed increases. Gaps do not erase consumed liquidity.
            self.spent[side] = {price: min(size, spent[price]) for price, size in self.levels_for(book, side)
                                if price in spent}

    def advance(self, at):
        while self.following and self.following[0] <= at:
            received, kind, payload = self.following
            self.following = next(self.frames, None)
            self.clock_at = received
            self.source.observe(self.generation, (self.slug, received, "stream", kind, payload))
            book, halted = self.source.snapshot(self.slug)
            self._sync_depth(book)
            self.current = book
            if halted:
                self.reason = "market_not_open"
            elif book is not None:
                self.reason = "stale_book"
            elif kind == "book":
                raw = recorded_book(received, payload)
                self.reason = "stale_book" if raw is not None and not usable(raw, received) else "invalidated_book"
            elif kind == "gap":
                self.reason = "stream_gap"
            elif kind == "trade" and self.reason not in ("no_stream_book", "stream_gap"):
                self.reason = "invalidated_book"
        self.clock_at = at
        return self.source.snapshot(self.slug)

    def quote(self, leg, at, depth_share):
        book, halted = self.advance(at)
        if halted:
            return ExitQuote("market_not_open", at)
        if book is None:
            if self.current is None:
                return ExitQuote(self.reason, at)
            return ExitQuote(self.reason, at, book_at=self.current.at, book_exchange_at=self.current.exchange_at)
        evidence = {"book_at": book.at, "book_exchange_at": book.exchange_at}
        if not usable(book, at, exchange_max_age=EXCHANGE_MAX_AGE) or book.exchange_at >= leg.window_end:
            return ExitQuote("stale_book", at, **evidence)

        bids = self.levels_for(book, leg.side)
        best_bid = bids[0][0] if bids else None
        total_visible = sum((size for _, size in bids), ZERO)
        if best_bid is not None and best_bid < leg.limit:
            return ExitQuote("price_below_limit", at, best_bid=best_bid, visible_depth=ZERO,
                             total_visible_depth=total_visible, executable_quantity=ZERO, **evidence)
        eligible = tuple((price, size) for price, size in bids if price >= leg.limit)
        visible, remaining, gross, fee = sum((size for _, size in eligible), ZERO), leg.quantity, ZERO, ZERO
        spent = self.spent[leg.side]
        levels = []
        for price, size in eligible:
            available = depth_share * max(ZERO, size - spent.get(price, ZERO))
            if self.quantity_step is not None:
                available = (available / self.quantity_step).to_integral_value(
                    rounding=ROUND_DOWN) * self.quantity_step
            quantity = min(remaining, available)
            if quantity <= ZERO:
                continue
            levels.append((price, quantity, size))
            gross += quantity * price
            remaining -= quantity
            if leg.fee_coefficient is not None:
                fee += leg.fee_coefficient * quantity * price * (1 - price)
            if remaining == ZERO:
                break
        details = {**evidence, "best_bid": best_bid, "total_visible_depth": total_visible, "visible_depth": visible,
                   "executable_quantity": leg.quantity - remaining}
        if remaining != ZERO:
            return ExitQuote("insufficient_depth", at, **details)
        for price, _, size in levels:
            spent[price] = size
        modeled_fee = fee.quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN) if leg.fee_coefficient is not None else None
        return ExitQuote("eligible", at, average_price=gross / leg.quantity, gross_proceeds=gross,
                         modeled_fee=modeled_fee, net_proceeds=gross - modeled_fee if modeled_fee is not None else None,
                         levels=tuple((price, quantity) for price, quantity, _ in levels), **details)


def benchmark_exit_books(tape_path: Path, legs, *, depth_share=DEFAULT_DEPTH_SHARE, wait_seconds=0, as_of=None):
    """Return one STREAM-only quote benchmark per leg, aligned to the input order.

    The default probe uses the logged send time. A wait probes a separate later
    instant. Displayed quotes are an immediate-execution benchmark, including
    for live orders that actually rested on the book.
    """
    depth_share = as_decimal(depth_share)
    if depth_share is None or not 0 < depth_share <= 1 or not isfinite(wait_seconds) or wait_seconds < 0:
        raise ValueError("Depth share must be in (0,1] and replay wait must be nonnegative")
    as_of = time.time() if as_of is None else as_of
    if not isfinite(as_of):
        raise ValueError("Replay cutoff must be finite")

    results = []
    by_slug = defaultdict(list)
    for index, leg in enumerate(legs):
        at = leg.sent_at + wait_seconds
        if (leg.side not in ("UP", "DOWN") or not leg.quantity.is_finite() or leg.quantity <= 0
                or not leg.limit.is_finite() or not 0 <= leg.limit <= 1 or not isfinite(at)
                or (leg.fee_coefficient is not None and
                    (not leg.fee_coefficient.is_finite() or leg.fee_coefficient < 0))):
            results.append(ExitQuote("invalid_leg", at))
        elif not leg.window_start <= leg.sent_at <= at < leg.window_end:
            results.append(ExitQuote("outside_window", at))
        elif at > as_of:
            results.append(ExitQuote("probe_after_cutoff", at))
        else:
            results.append(ExitQuote("market_not_recorded", at))
            by_slug[leg.slug].append((at, index, leg))
    if not tape_path.is_file() or not by_slug:
        return results

    with readonly(tape_path) as db:
        db.execute("BEGIN")
        for slug, probes in by_slug.items():
            row = db.execute("SELECT slug,start,end,window FROM markets WHERE slug=?", (slug,)).fetchone()
            window = decode_window(row) if row is not None else None
            if window is None:
                continue
            replay = _BookReplay(window, _stream_frames(db, window, max(at for at, _, _ in probes)))
            for at, index, leg in sorted(probes):
                if (leg.window_start, leg.window_end) == (window.start, window.end):
                    results[index] = replay.quote(leg, at, depth_share)
                else:
                    results[index] = ExitQuote("window_mismatch", at)
    return results
