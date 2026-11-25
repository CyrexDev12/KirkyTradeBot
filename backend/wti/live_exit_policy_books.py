"""Recorded WebSocket observations and conservative depth for Oil D exit policies."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from itertools import groupby
from math import isfinite, nextafter
from pathlib import Path

from .live_books import BOOK_MAX_AGE, EXCHANGE_MAX_AGE, usable
from .live_exit_replay_books import DEFAULT_DEPTH_SHARE, _stream_frames
from .live_timing import as_decimal, readonly
from .replay_capture import StreamBookSource
from .shadow import decode_window


ZERO = Decimal(0)


def load_exit_books(tape: Path, trade, as_of: float):
    """Read one confirmed trade's exact WTI 15-minute WS window from the tape."""
    tape = Path(tape)
    if not tape.is_file() or not isfinite(as_of):
        return None, []
    if isinstance(trade, Mapping):
        slug = trade.get("slug") or trade.get("market_slug")
        start, end = trade.get("window_start"), trade.get("window_end")
    else:
        slug, start, end = trade.slug, trade.window_start, trade.window_end
    with readonly(tape) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT slug,start,end,window FROM markets WHERE slug=?", (slug,)).fetchone()
        window = decode_window(row) if row is not None else None
        if window is None or (window.start, window.end) != (start, end):
            return None, []
        frames = _stream_frames(db, window, min(as_of, window.end))
        return window, [frame for frame in frames if frame[0] < window.end]


def exit_timeline(window, frames, *, as_of=None):
    """Yield (at, book, market_not_open) with original book timestamps.

    Quotes remain usable through their receipt/exchange age limit. A no-book
    event marks expiry before the next recorded update so dwell cannot bridge it.
    """
    end = window.end if as_of is None else min(window.end, as_of)
    at = window.start
    source = StreamBookSource(clock=lambda: at)
    generation = source.begin(window)
    source.set_connected(generation, True)
    expiry = None
    for received, batch in groupby(frames, key=lambda frame: frame[0]):
        if received > end or received >= window.end:
            break
        if expiry is not None and expiry < received:
            yield expiry, None, False
        at = received
        for _, kind, payload in batch:
            source.observe(generation, (window.slug, at, "stream", kind, payload))
        book, halted = source.snapshot(window.slug)
        if book is not None and (
            not usable(book, at, exchange_max_age=EXCHANGE_MAX_AGE) or book.exchange_at >= window.end
        ):
            book = None
        expiry = None
        if book is not None:
            limit = min(book.at + BOOK_MAX_AGE, book.exchange_at + EXCHANGE_MAX_AGE)
            expiry = nextafter(limit, float("inf"))
        yield at, book, halted
    if expiry is not None and expiry <= end and expiry < window.end:
        yield expiry, None, False


@dataclass(frozen=True)
class DepthFill:
    quantity: Decimal
    average_price: Decimal
    gross_proceeds: Decimal
    levels: tuple[tuple[Decimal, Decimal], ...]


class BookDepth:
    """Charge a filled price level to this scenario until observed depth replenishes."""

    def __init__(self, depth_share=DEFAULT_DEPTH_SHARE, quantity_step=None):
        self.share = as_decimal(depth_share)
        self.step = as_decimal(quantity_step)
        if self.share is None or not 0 < self.share <= 1:
            raise ValueError("Depth share must be in (0, 1]")
        if quantity_step is not None and (self.step is None or self.step <= 0):
            raise ValueError("Quantity increment must be positive")
        self.last = None
        self.spent = {"UP": {}, "DOWN": {}}

    @staticmethod
    def levels_for(book, side):
        return tuple((Decimal(str(price)), Decimal(str(size))) for price, size in book.side_bids(side))

    def observe(self, book):
        """Update physical depth from a verified full observation, keeping spent depth through gaps."""
        if book is None or book.exchange_at is None:
            return False
        if self.last is not None:
            if book == self.last:
                return True
            if (book.at < self.last.at or book.exchange_at < self.last.exchange_at
                    or (book.sequence is not None and self.last.sequence is not None and book.sequence < self.last.sequence)):
                return False
            if (book.exchange_at == self.last.exchange_at
                    and (book.sequence is None or self.last.sequence is None or book.sequence <= self.last.sequence)):
                return False
        for side, used in self.spent.items():
            self.spent[side] = {price: min(size, used[price]) for price, size in self.levels_for(book, side)
                                if price in used}
        self.last = book
        return True

    def take(self, book, side, quantity, limit=ZERO):
        """Take up to the requested amount at visible bids meeting the limit."""
        quantity, limit = as_decimal(quantity), as_decimal(limit)
        if (side not in self.spent or quantity is None or quantity <= 0 or limit is None
                or not 0 <= limit <= 1 or not self.observe(book)):
            return None
        if self.step is not None:
            quantity = (quantity / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
        if quantity <= ZERO:
            return None
        remaining, gross = quantity, ZERO
        fills = []
        for price, size in self.levels_for(book, side):
            if price < limit:
                break
            available = self.share * max(ZERO, size - self.spent[side].get(price, ZERO))
            if self.step is not None:
                available = (available / self.step).to_integral_value(rounding=ROUND_DOWN) * self.step
            amount = min(remaining, available)
            if amount <= 0:
                continue
            self.spent[side][price] = size
            remaining -= amount
            gross += amount * price
            fills.append((price, amount))
            if remaining == 0:
                break
        filled = quantity - remaining
        return DepthFill(filled, gross / filled, gross, tuple(fills)) if filled else None
