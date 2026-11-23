"""Read-only Polymarket US market-data client and REST recorder.

PAPER_ONLY guard: :class:`ReadOnlyVenueClient` will only issue HTTP GET requests
to the allow-listed public market-data routes below. Any other method or path,
including every order-submission route, raises :class:`PaperOnlyViolation`
before a request is built. There is no code path in this package that places,
cancels or amends a real order.

Routes (https://gateway.polymarket.us, reviewed 2026-11-17; verify pagination,
field meanings and availability against captured responses):

* GET /v2/leagues/nfl/events
* GET /v1/market/slug/{slug}
* GET /v1/markets/{slug}/bbo
* GET /v1/markets/{slug}/book
* GET /v1/price-history?symbol={slug}&...   (book-derived DISPLAY prices, not trades)
* GET /v1/markets/{slug}/settlement
* GET /v1/events?slug={slug}                (crypto Up/Down windows)
* GET /v1/asset-prices/history?assets=...   (settlement index prices)

The authenticated market WebSocket is optional and not implemented here; the
public REST collector is the default.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import random
import re
import time

import httpx

from .book import parse_book

BASE = "https://gateway.polymarket.us"
_SLUG = r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,200}"
ALLOWED_GET = tuple(re.compile(p) for p in (
    r"^/v2/leagues/nfl/events$",
    rf"^/v1/market/slug/{_SLUG}$",
    rf"^/v1/markets/{_SLUG}/(bbo|book|settlement)$",
    r"^/v1/price-history$",
    r"^/v1/events/[A-Za-z0-9_\-]{1,100}$",
    r"^/v1/events$",  # ?slug=... lookup used by the crypto Up/Down paper bot
    r"^/v1/asset-prices/history$",  # settlement-index (BRTI) price history
))


class PaperOnlyViolation(RuntimeError):
    pass


def check_request(method: str, path: str) -> None:
    if method.upper() != "GET":
        raise PaperOnlyViolation(f"PAPER_ONLY: refusing {method} {path}")
    if not any(p.match(path) for p in ALLOWED_GET):
        raise PaperOnlyViolation(f"PAPER_ONLY: {path} is not an allow-listed public market-data route")


class ReadOnlyVenueClient:
    def __init__(self, client: httpx.AsyncClient | None = None, min_interval: float = 0.6, max_retries: int = 3,
                 clock=lambda: datetime.now(timezone.utc)):
        self.client = client or httpx.AsyncClient(base_url=BASE, timeout=15,
                                                   limits=httpx.Limits(max_connections=4))
        self.min_interval, self.max_retries, self.clock = min_interval, max_retries, clock
        self._lock = asyncio.Lock()
        self._next = 0.0
        self.last_error: str | None = None

    async def request(self, method: str, path: str, params: dict | None = None) -> dict:
        check_request(method, path)
        for attempt in range(self.max_retries + 1):
            async with self._lock:
                wait = self._next - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next = time.monotonic() + self.min_interval * (1 + random.random() * 0.25)
            response = await self.client.get(path, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() \
                    else min(30.0, 2 ** attempt) * (1 + random.random() * 0.5)
                self.last_error = f"HTTP {response.status_code}"
                if attempt < self.max_retries:
                    await asyncio.sleep(delay)
                    continue
            response.raise_for_status()
            self.last_error = None
            return response.json()
        raise RuntimeError("unreachable")

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self.request("GET", path, params)

    async def book(self, slug: str):
        payload = await self.get(f"/v1/markets/{slug}/book")
        return parse_book(slug, payload, self.clock())

    async def settlement(self, slug: str) -> dict:
        return await self.get(f"/v1/markets/{slug}/settlement")

    async def price_history(self, slug: str, **params) -> dict:
        return await self.get("/v1/price-history", {"symbol": slug, **params})

    async def close(self):
        await self.client.aclose()


class MarketRecorder:
    """Polls active books often and idle candidates rarely; records every snapshot."""

    def __init__(self, venue: ReadOnlyVenueClient, store, active_seconds: int = 5, idle_seconds: int = 60):
        self.venue, self.store = venue, store
        self.active_seconds, self.idle_seconds = active_seconds, idle_seconds
        self.last_poll: dict[str, float] = {}
        self.errors: dict[str, str] = {}

    def due(self, slugs_active, slugs_idle, now_monotonic: float) -> list[str]:
        out = []
        for slug in sorted(set(slugs_active) | set(slugs_idle)):
            period = self.active_seconds if slug in slugs_active else self.idle_seconds
            if now_monotonic - self.last_poll.get(slug, -1e9) >= period:
                out.append(slug)
        return out

    async def poll(self, slugs_active, slugs_idle) -> list:
        snaps = []
        for slug in self.due(slugs_active, slugs_idle, time.monotonic()):
            self.last_poll[slug] = time.monotonic()
            try:
                snap = await self.venue.book(slug)
            except Exception as exc:  # recorded as degraded status; never invents a book
                self.errors[slug] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            self.errors.pop(slug, None)
            self.store.record_snapshot(snap)
            snaps.append(snap)
        return snaps

    async def backfill_display_history(self, slug: str, game_id: str, **params) -> int:
        """Store official price-history DISPLAY prices separately. They never feed fills or labels."""
        payload = await self.venue.price_history(slug, **params)
        self.store.record_market_event(slug, game_id, "display_price_history", self.venue.clock(), payload)
        return 1
