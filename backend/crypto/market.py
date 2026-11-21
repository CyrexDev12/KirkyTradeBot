"""Public Coinbase Exchange BTC-USD data. No credentials or order endpoints."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

import httpx

UTC = timezone.utc
PRODUCT = "BTC-USD"
ROOT = "https://api.exchange.coinbase.com/products/BTC-USD"
HISTORY_BARS = 96  # Eight hours of completed five-minute buckets.


class DataUnavailable(ValueError):
    """An input could not be verified for the requested decision time."""


@dataclass(frozen=True)
class Bar:
    start: int
    low: float
    high: float
    open: float
    close: float


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    at: datetime

    def validate(self, now: datetime):
        if not all(math.isfinite(x) and x > 0 for x in (self.bid, self.ask)) or self.bid > self.ask:
            raise DataUnavailable("invalid Coinbase bid/ask")
        if self.at.tzinfo is None:
            raise DataUnavailable("Coinbase ticker missing timezone")
        age = (now - self.at).total_seconds()
        if age < -2:
            raise DataUnavailable(f"Coinbase ticker is {-age:.1f}s ahead of worker clock; check VM time sync")
        if age > 20:
            raise DataUnavailable(f"stale Coinbase ticker ({age:.1f}s old)")


def closed_bars(rows, slot: datetime) -> list[Bar]:
    """Require a contiguous, complete window ending at the scheduled slot."""
    cutoff = int(slot.timestamp())
    start = cutoff - HISTORY_BARS * 300
    selected = {}
    for raw in rows:
        try:
            stamp = int(raw[0])
            if stamp != float(raw[0]):
                raise DataUnavailable("fractional Coinbase bucket timestamp")
            if stamp < start or stamp >= cutoff:
                continue  # Coinbase may return buckets outside the requested interval.
            low, high, opening, close = map(float, raw[1:5])
        except (ValueError, TypeError, IndexError, KeyError, OverflowError) as exc:
            raise DataUnavailable("malformed Coinbase candle") from exc
        if (stamp % 300 or stamp in selected or
                not all(math.isfinite(p) and p > 0 for p in (low, high, opening, close)) or
                not low <= min(opening, close) <= max(opening, close) <= high):
            raise DataUnavailable("invalid Coinbase candle")
        selected[stamp] = Bar(stamp, low, high, opening, close)
    expected = range(start, cutoff, 300)
    if any(stamp not in selected for stamp in expected):
        raise DataUnavailable("missing completed Coinbase candles")
    return [selected[stamp] for stamp in expected]


class Market:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def bars(self, slot):
        response = await self.client.get(f"{ROOT}/candles", params={
            "granularity": 300, "start": (slot - timedelta(seconds=HISTORY_BARS * 300)).isoformat(),
            "end": slot.isoformat(),
        })
        response.raise_for_status()
        return closed_bars(response.json(), slot)

    async def quote(self):
        response = await self.client.get(f"{ROOT}/ticker")
        response.raise_for_status()
        item = response.json()
        try:
            return Quote(float(item["bid"]), float(item["ask"]), datetime.fromisoformat(item["time"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DataUnavailable("malformed Coinbase ticker") from exc
