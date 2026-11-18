"""Normalized order-book snapshots and selected-outcome views.

A Polymarket US two-sided market is one canonical instrument whose YES side is
the ``long`` market side. Selected-outcome prices are derived explicitly:

* selected YES entry = canonical YES ask; liquidation = canonical YES bid
* selected NO entry  = 1 - canonical YES bid; liquidation = 1 - canonical YES ask

Quantities come only from full-book price levels. ``bidDepth``/``askDepth``
counts are never treated as contract quantity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .common import ONE, ZERO, D, utc

YES = "YES"
NO = "NO"

OPEN = "OPEN"
HALTED = "HALTED"
CLOSED = "CLOSED"
RESOLVED = "RESOLVED"
UNKNOWN = "UNKNOWN"

_STATE_MAP = {
    "MARKET_STATE_OPEN": OPEN,
    "MARKET_STATE_HALTED": HALTED,
    "MARKET_STATE_PAUSED": HALTED,
    "MARKET_STATE_SUSPENDED": HALTED,
    "MARKET_STATE_CLOSED": CLOSED,
    "MARKET_STATE_EXPIRED": CLOSED,
    "MARKET_STATE_RESOLVED": RESOLVED,
    "MARKET_STATE_SETTLED": RESOLVED,
}


@dataclass(frozen=True)
class BookSnapshot:
    slug: str
    received_at: datetime
    exchange_ts: datetime | None
    state: str
    bids: tuple  # canonical YES bids, ((price, qty), ...) best first (descending)
    offers: tuple  # canonical YES offers, best first (ascending)
    sequence: int | None = None
    source: str = "rest_book"
    quality_flags: frozenset = field(default_factory=frozenset)

    def age_seconds(self) -> float | None:
        if self.exchange_ts is None:
            return None
        return (self.received_at - self.exchange_ts).total_seconds()

    def to_dict(self) -> dict:
        return {"slug": self.slug, "received_at": self.received_at.isoformat(),
                "exchange_ts": self.exchange_ts.isoformat() if self.exchange_ts else None,
                "state": self.state, "sequence": self.sequence, "source": self.source,
                "bids": [[str(p), str(q)] for p, q in self.bids],
                "offers": [[str(p), str(q)] for p, q in self.offers],
                "quality_flags": sorted(self.quality_flags)}

    @classmethod
    def from_dict(cls, data: dict) -> "BookSnapshot":
        return cls(slug=data["slug"], received_at=utc(data["received_at"]),
                   exchange_ts=utc(data["exchange_ts"]) if data.get("exchange_ts") else None,
                   state=data["state"], sequence=data.get("sequence"), source=data.get("source", "rest_book"),
                   bids=tuple((D(p), D(q)) for p, q in data["bids"]),
                   offers=tuple((D(p), D(q)) for p, q in data["offers"]),
                   quality_flags=frozenset(data.get("quality_flags") or ()))


@dataclass(frozen=True)
class SideBook:
    """Book from the selected outcome's perspective: buy at ``asks``, sell into ``bids``."""
    bids: tuple
    asks: tuple

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None


def _price(value) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        result = D(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if ZERO < result < ONE else None


def _qty(value) -> Decimal | None:
    try:
        result = D(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result > 0 else None


def _levels(items, descending: bool) -> tuple:
    merged: dict[Decimal, Decimal] = {}
    for item in items or []:
        p, q = _price(item.get("px")), _qty(item.get("qty"))
        if p is not None and q is not None:
            merged[p] = merged.get(p, ZERO) + q
    return tuple(sorted(merged.items(), key=lambda x: x[0], reverse=descending))


def parse_book(slug: str, payload: dict, received_at: datetime, source: str = "rest_book") -> BookSnapshot:
    """Parse a ``GET /v1/markets/{slug}/book`` response (``marketData`` wrapper optional)."""
    data = payload.get("marketData", payload) if isinstance(payload, dict) else {}
    flags = set()
    raw_state = data.get("state")
    state = _STATE_MAP.get(raw_state, UNKNOWN)
    if state == UNKNOWN:
        flags.add(f"unknown_state:{raw_state}")
    ts = data.get("transactTime")
    exchange_ts = None
    if ts:
        try:
            exchange_ts = utc(ts)
        except ValueError:
            flags.add("bad_exchange_timestamp")
    else:
        flags.add("no_exchange_timestamp")
    bids, offers = _levels(data.get("bids"), True), _levels(data.get("offers"), False)
    if bids and offers and bids[0][0] >= offers[0][0]:
        flags.add("crossed_book")
    seq = data.get("sequence") or data.get("seqNum")
    return BookSnapshot(slug=slug, received_at=utc(received_at), exchange_ts=exchange_ts, state=state,
                        bids=bids, offers=offers, sequence=int(seq) if seq is not None else None,
                        source=source, quality_flags=frozenset(flags))


def selected_view(snapshot: BookSnapshot, side: str) -> SideBook:
    if side == YES:
        return SideBook(bids=snapshot.bids, asks=snapshot.offers)
    if side == NO:
        return SideBook(bids=tuple((ONE - p, q) for p, q in snapshot.offers),
                        asks=tuple((ONE - p, q) for p, q in snapshot.bids))
    raise ValueError(f"Unknown side {side}")


def is_fresh(snapshot: BookSnapshot, now: datetime, max_age_seconds: int) -> tuple[bool, str | None]:
    """Freshness is judged by the successful book refresh, not by an unchanged trade timestamp."""
    if snapshot.state == UNKNOWN:
        return False, "unknown market state"
    if "crossed_book" in snapshot.quality_flags:
        return False, "crossed book"
    if (now - snapshot.received_at).total_seconds() > max_age_seconds:
        return False, "book refresh too old"
    return True, None
