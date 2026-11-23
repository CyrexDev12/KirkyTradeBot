"""Read-only inputs for the Up/Down bot: Polymarket US windows, books and index
prices (through the PAPER_ONLY-guarded venue client) and Coinbase tickers.

Shapes were checked against live responses on 2026-11-22:

* ``GET /v1/events?slug=btc-updown-15m-2026-11-22-1915z`` — the slug carries the
  window START in UTC; ``events[0].markets[0]`` holds ``status``,
  ``feeCoefficient`` and ``assetPriceTerms`` (``windowStart``, ``windowEnd``,
  ``priceToBeat.value``, ``settlementPrice.value`` once resolved).
* ``GET /v1/markets/{slug}/settlement`` — published payout per YES contract on the resolved market.
* ``GET /v1/markets/{slug}/book`` — YES (Up) book, bids best-first, offers best-first.
* ``GET /v1/asset-prices/history?assets=crypto:btc&bucket=ASSET_PRICE_BUCKET_1S&from=..&to=0``
  — ``series[].candles[{timestamp, close, tickCount}]`` with ``timestamp`` = bucket start.
* Gold (``gold-updown-15m-…``, ``gold-updown-1h-…``; ``assetClass`` COMMODITY, history id
  ``commodity:gold``): the price to beat equals the 1-second close at ``windowStart`` and the
  settlement price the 1-second close at ``windowEnd`` (Chainlink XAU/USD, checked on the
  2026-11-20 18:00Z windows). No 5-minute gold series was listed on 2026-11-22; the bot
  looks it up anyway and simply finds nothing to trade until one appears.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time

import httpx

from .strategy import Book, Product, Window

COINBASE = "https://api.exchange.coinbase.com/products/{}/ticker"
FINAL_MARKET_STATUSES = frozenset(("MARKET_STATUS_RESOLVED", "MARKET_STATUS_CANCELED",
                                  "MARKET_STATUS_CANCELLED", "MARKET_STATUS_VOIDED"))


class FeedError(ValueError):
    """An input could not be verified; the bot skips rather than guess."""


def window_start(now: float, seconds: int = 900) -> int:
    return int(now // seconds * seconds)


def event_slug(asset: str, start: int, horizon: str = "15m") -> str:
    return f"{asset}-updown-{horizon}-" + datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d-%H%Mz")


def _ts(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _value(obj):
    if isinstance(obj, dict) and obj.get("value") not in (None, ""):
        return float(obj["value"])
    return None


def parse_window(asset: str, payload: dict, default_theta: float = 0.095, rule: str = "avg60") -> Window:
    try:
        market = payload["events"][0]["markets"][0]
        terms = market["assetPriceTerms"]
    except (KeyError, IndexError, TypeError) as exc:
        raise FeedError(f"no {asset} Up/Down window") from exc
    if terms.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN":
        raise FeedError("not an Up/Down market")
    return Window(asset=asset, slug=market["slug"], start=_ts(terms["windowStart"]), end=_ts(terms["windowEnd"]),
                  price_to_beat=_value(terms.get("priceToBeat")), status=market.get("status", ""),
                  theta=float(market.get("feeCoefficient") or default_theta),
                  horizon=terms.get("horizon") or "15m", rule=rule)


def settlement_price(payload: dict) -> tuple[str, float | None]:
    market = payload["events"][0]["markets"][0]
    status = market.get("status", "")
    if status not in FINAL_MARKET_STATUSES:
        return status, None
    price = _value(market["assetPriceTerms"].get("settlementPrice"))
    return status, price if price is not None and math.isfinite(price) else None


@dataclass(frozen=True)
class Settlement:
    """The exchange's contract payout and optional published underlying prices."""

    slug: str
    yes_payout: float
    price_to_beat: float | None
    price: float | None

    @property
    def outcome(self):
        return "UP" if self.yes_payout == 1 else "DOWN" if self.yes_payout == 0 else "OFFICIAL_PAYOUT"


def _official_payout(payload, slug):
    if not isinstance(payload, dict) or payload.get("slug") != slug:
        raise FeedError("settlement response market does not match")
    raw = payload.get("settlement")
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise FeedError("invalid settlement payout")
    try:
        payout = float(raw)
    except (ValueError, TypeError, OverflowError) as exc:
        raise FeedError("invalid settlement payout") from exc
    if not 0 <= payout <= 1:
        raise FeedError("invalid settlement payout")
    return payout


def parse_book(payload: dict, at: float, *, requested_at=None, slug=None) -> Book:
    data = payload.get("marketData") or {}
    if data.get("state") != "MARKET_STATE_OPEN":
        raise FeedError(f"book {data.get('state')}")
    if slug is not None and data.get("marketSlug") not in (None, slug):
        raise FeedError("book market does not match the order")

    def levels(rows, reverse):
        out = []
        seen = set()
        for row in rows or ():
            try:
                price, qty = float(row["px"]["value"]), float(row["qty"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise FeedError("invalid book level") from exc
            if 0 < price < 1 and math.isfinite(qty) and qty > 0:
                if price in seen:
                    raise FeedError("duplicate book price level")
                seen.add(price)
                out.append((price, qty))
        return tuple(sorted(out, reverse=reverse))

    bids = levels(data.get("bids"), True)
    offers = levels(data.get("offers"), False)
    if bids and offers and bids[0][0] >= offers[0][0]:
        raise FeedError("crossed or locked book")
    timestamp = data.get("transactTime")
    sequence = data.get("sequence", data.get("seqNum"))
    try:
        exchange_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
        if exchange_at is not None and exchange_at.tzinfo is None:
            raise ValueError("exchange timestamp has no timezone")
        sequence = int(sequence) if sequence is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise FeedError("invalid order book timestamp or sequence") from exc
    return Book(bids, offers, at, exchange_at.timestamp() if exchange_at else None, requested_at, sequence)


def parse_index(payload: dict) -> dict[str, list[tuple[int, float]]]:
    """{asset: [(bucket_start, close), ...]} skipping gap-filled buckets."""
    out = {}
    for series in payload.get("series", ()):
        symbol = series["asset"]["symbol"].lower()
        rows = []
        for c in series.get("candles", ()):
            if c.get("tickCount", 1) == 0:
                continue
            close = float(c["close"])
            if math.isfinite(close) and close > 0:
                rows.append((int(c["timestamp"]), close))
        out[symbol] = sorted(rows)
    return out


def point_price(seconds: list[tuple[int, float]], at: int) -> float | None:
    """Latest observed 1-second close at or before ``at`` (within 5 s)."""
    vals = [p for t, p in seconds if at - 5 <= t <= at]
    return vals[-1] if vals else None


def closing_minute_average(seconds: list[tuple[int, float]], end: int, now: float) -> float | None:
    """Average of the observed 1-second prices in [end-60, now)."""
    vals = [p for t, p in seconds if end - 60 <= t < min(now, end)]
    return sum(vals) / len(vals) if vals else None


class Feed:
    def __init__(self, venue, http: httpx.AsyncClient, clock=time.time):
        self.venue, self.http, self.clock = venue, http, clock
        self._minutes: tuple[int, dict] | None = None

    async def _event(self, product: Product, start: int):
        payload = await self.venue.get("/v1/events", {"slug": event_slug(product.symbol, start, product.horizon)})
        if not payload.get("events"):
            raise FeedError(f"no {product.key} market listed for this window")
        return payload

    async def window(self, product: Product, start: int) -> Window:
        return parse_window(product.symbol, await self._event(product, start), rule=product.rule)

    async def settlement(self, product: Product, start: int) -> Settlement | None:
        event = await self._event(product, start)
        market = event["events"][0]["markets"][0]
        if market.get("status") not in FINAL_MARKET_STATUSES:
            return None
        window = parse_window(product.symbol, event, rule=product.rule)
        terms = market["assetPriceTerms"]
        if (window.start != start or window.horizon != product.horizon
                or (terms.get("asset") or {}).get("symbol") != product.symbol or self.clock() < window.end):
            raise FeedError("settlement event differs from the expected finished window")
        payload = await self.venue.get(f"/v1/markets/{window.slug}/settlement")
        payout = _official_payout(payload, window.slug)
        if payout is None:
            return None
        _, price = settlement_price(event)
        ptb = window.price_to_beat
        return Settlement(window.slug, payout, ptb if ptb is not None and math.isfinite(ptb) else None, price)

    async def book(self, slug: str) -> Book:
        requested_at = self.clock()
        payload = await self.venue.get(f"/v1/markets/{slug}/book")
        return parse_book(payload, self.clock(), requested_at=requested_at, slug=slug)

    async def _history(self, index_ids, bucket: str, start: int):
        params = [("assets", i) for i in index_ids] + [
            ("bucket", bucket), ("from", str(start)), ("to", "0")]
        return parse_index(await self.venue.get("/v1/asset-prices/history", params))

    async def seconds(self, index_ids, lookback: int = 120):
        return await self._history(index_ids, "ASSET_PRICE_BUCKET_1S", int(self.clock()) - lookback)

    async def minutes(self, index_ids, lookback: int = 7200):
        """One-minute closes for volatility, refreshed once a minute."""
        minute = int(self.clock() // 60)
        if self._minutes and self._minutes[0] == minute:
            return self._minutes[1]
        data = await self._history(index_ids, "ASSET_PRICE_BUCKET_1M", int(self.clock()) - lookback)
        self._minutes = (minute, data)
        return data

    async def coinbase(self, product_id: str) -> float:
        response = await self.http.get(COINBASE.format(product_id))
        response.raise_for_status()
        data = response.json()
        bid, ask = float(data["bid"]), float(data["ask"])
        stamp = datetime.fromisoformat(data["time"].replace("Z", "+00:00")).timestamp()
        if not 0 < bid <= ask or abs(self.clock() - stamp) > 20:
            raise FeedError(f"stale or invalid Coinbase {product_id} ticker")
        return (bid + ask) / 2
