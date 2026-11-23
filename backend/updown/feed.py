"""Read-only inputs for the Up/Down bot: Polymarket US windows, books and index
prices (through the PAPER_ONLY-guarded venue client) and Coinbase tickers.

Shapes were checked against live responses on 2026-11-22:

* ``GET /v1/events?slug=btc-updown-15m-2026-11-22-1915z`` — the slug carries the
  window START in UTC; ``events[0].markets[0]`` holds ``status``,
  ``feeCoefficient`` and ``assetPriceTerms`` (``windowStart``, ``windowEnd``,
  ``priceToBeat.value``, ``settlementPrice.value`` once resolved).
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

from datetime import datetime, timezone
import math
import time

import httpx

from .strategy import Book, Product, Window

COINBASE = "https://api.exchange.coinbase.com/products/{}/ticker"


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
    return market.get("status", ""), _value(market["assetPriceTerms"].get("settlementPrice"))


def parse_book(payload: dict, at: float) -> Book:
    data = payload.get("marketData") or {}
    if data.get("state") not in (None, "MARKET_STATE_OPEN"):
        raise FeedError(f"book {data.get('state')}")

    def levels(rows, reverse):
        out = []
        for row in rows or ():
            price, qty = float(row["px"]["value"]), float(row["qty"])
            if 0 < price < 1 and qty > 0:
                out.append((price, qty))
        return tuple(sorted(out, reverse=reverse))

    return Book(levels(data.get("bids"), True), levels(data.get("offers"), False), at)


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

    async def settlement(self, product: Product, start: int):
        return settlement_price(await self._event(product, start))

    async def book(self, slug: str) -> Book:
        return parse_book(await self.venue.get(f"/v1/markets/{slug}/book"), self.clock())

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
