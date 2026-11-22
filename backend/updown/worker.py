"""PAPER_ONLY Up/Down worker: every few seconds, price each open 15-minute window,
simulate entries/early exits against the live book, and settle finished windows.

Run: ``python -m backend.updown.worker`` (writes ``data/updown/health.json``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal as os_signal
import time

import httpx

from ..trading.venue import BASE, ReadOnlyVenueClient
from .feed import Feed, FeedError, closing_minute_average, window_start
from .model import fair_up, sigma_per_second
from .store import Store, data_directory
from .strategy import SETTINGS, Settings, early_exit, evaluate

logger = logging.getLogger(__name__)
FALLBACK_AFTER = 1800  # seconds after window end before using the index-derived close


class Worker:
    def __init__(self, store: Store, feed: Feed, settings: Settings = SETTINGS, clock=time.time):
        self.store, self.feed, self.settings, self.clock = store, feed, settings, clock
        self.windows: dict[tuple, object] = {}
        self.errors: dict[str, str] = {}
        self._settle_checked: dict[tuple, float] = {}

    async def _window(self, asset, start):
        key = (asset, start)
        cached = self.windows.get(key)
        if cached is None or cached.price_to_beat is None:
            cached = await self.feed.window(asset, start)
            self.windows[key] = cached
            for old in [k for k in self.windows if k[1] < start - 900]:
                self.windows.pop(old)
        return cached

    async def tick(self):
        now = self.clock()
        start = window_start(now)
        assets = self.settings.assets
        try:
            seconds = await self.feed.seconds(assets)
            minutes = await self.feed.minutes(assets)
        except (httpx.HTTPError, FeedError, ValueError, KeyError) as exc:
            self.errors["index"] = f"{type(exc).__name__}: {exc}"[:200]
            return
        self.errors.pop("index", None)
        for trade in self.store.open_trades():
            await self._manage(trade, seconds, minutes)
        for asset in assets:
            try:
                await self._consider(asset, start, seconds.get(asset), minutes.get(asset))
                self.errors.pop(asset, None)
            except (httpx.HTTPError, FeedError, ValueError, KeyError) as exc:
                self.errors[asset] = f"{type(exc).__name__}: {exc}"[:200]
                self.store.note_window(asset, start, f"data unavailable: {type(exc).__name__}", self.clock())
        await self._resolve_windows()

    def _fair(self, window, seconds, minutes, now):
        if not seconds or not minutes:
            raise FeedError("missing index prices")
        if now - seconds[-1][0] > 10:
            raise FeedError("stale settlement-index price")
        sigma = sigma_per_second([c for _, c in minutes[-121:]])
        avg = closing_minute_average(seconds, window.end, now) if window.end - now <= 60 else None
        return fair_up(window.price_to_beat, seconds[-1][1], window.end - now, sigma, avg), seconds[-1][1], sigma, avg

    async def _consider(self, asset, start, seconds, minutes):
        window = await self._window(asset, start)
        now = self.clock()
        held = self.store.held(asset, start)
        if held or window.price_to_beat is None or now < window.start + self.settings.entry_after_seconds - 5:
            if not held:
                self.store.note_window(asset, start, "waiting for the price to beat / entry part", now,
                                       window.price_to_beat)
            return
        book = await self.feed.book(window.slug)
        coinbase = None
        try:
            coinbase = await self.feed.coinbase(asset)
        except (httpx.HTTPError, FeedError, ValueError, KeyError) as exc:
            self.errors[f"coinbase-{asset}"] = f"{type(exc).__name__}: {exc}"[:200]
        now = self.clock()
        if not seconds or not minutes:
            raise FeedError("missing index prices")
        sigma = sigma_per_second([c for _, c in minutes[-121:]])
        index_price = seconds[-1][1]
        if now - seconds[-1][0] > 10:
            raise FeedError("stale settlement-index price")
        avg = closing_minute_average(seconds, window.end, now) if window.end - now <= 60 else None
        snap = self.store.snapshot(now)
        decision = evaluate(window, book, now, index_price, sigma, avg, coinbase, snap["balance"],
                            snap["open_stake"], snap["today_pnl"], held, self.settings)
        self.store.observe(asset, start, now, seconds_left=window.end - now, index_price=index_price,
                           coinbase=coinbase, fair_up=decision.fair_up,
                           yes_bid=book.yes_bids[0][0] if book.yes_bids else None,
                           yes_ask=book.yes_offers[0][0] if book.yes_offers else None)
        if decision.action == "BUY":
            trade = self.store.open(window, decision, now, {"index_price": index_price, "coinbase_price": coinbase,
                                                            "sigma_per_second": sigma,
                                                            "seconds_left": round(window.end - now, 1)})
            if trade:
                logger.info("PAPER BUY %s %s x%s @ %.3f", asset, decision.side, decision.quantity, decision.price)
        self.store.note_window(asset, start, decision.reason, now, window.price_to_beat)

    async def _manage(self, trade, seconds, minutes):
        asset, start, end = trade["asset"], trade["window_start_ts"], trade["window_end_ts"]
        now = self.clock()
        try:
            if now < end:
                window = await self._window(asset, start)
                book = await self.feed.book(trade["market_slug"])
                p_up = self._fair(window, seconds.get(asset), minutes.get(asset), self.clock())[0]
                sold = early_exit(trade["side"], trade["remaining"], book, p_up, trade["theta"], self.settings)
                if sold:
                    self.store.partial_exit(trade["id"], sold, self.clock())
                return
            key = (asset, start)
            if now - self._settle_checked.get(key, 0) < 15:
                return
            self._settle_checked[key] = now
            status, price = await self.feed.settlement(asset, start)
            if price is not None:
                self.store.settle(trade["id"], price, self.clock())
                self.store.record_outcome(asset, start, trade["price_to_beat"], price)
                self._settle_checked.pop(key, None)
            elif now >= end + FALLBACK_AFTER:
                close = await self._index_close(asset, end)
                if close is not None:
                    self.store.settle(trade["id"], close, self.clock(), source="index fallback")
                    self.store.record_outcome(asset, start, trade["price_to_beat"], close)
        except (httpx.HTTPError, FeedError, ValueError, KeyError) as exc:
            self.errors[f"trade-{trade['id']}"] = f"{type(exc).__name__}: {exc}"[:200]

    async def _index_close(self, asset, end):
        rows = (await self.feed.seconds([asset], lookback=int(self.clock() - end) + 70)).get(asset, [])
        vals = [p for t, p in rows if end - 60 <= t < end]
        return round(sum(vals) / len(vals), 2) if len(vals) >= 50 else None

    async def _resolve_windows(self):
        """Record outcomes of windows the bot watched but did not trade (for calibration)."""
        now = self.clock()
        for asset, start, ptb in self.store.unresolved_windows(int(now) - 60):
            key = (asset, start)
            if now - self._settle_checked.get(key, 0) < 60 or ptb is None:
                continue
            self._settle_checked[key] = now
            try:
                _, price = await self.feed.settlement(asset, start)
            except (httpx.HTTPError, FeedError, ValueError, KeyError):
                continue
            if price is not None:
                self.store.record_outcome(asset, start, ptb, price)
                self._settle_checked.pop(key, None)
            elif now > start + 900 + 3 * 3600:
                self.store.record_outcome(asset, start, ptb, ptb)  # unknown; stop polling
                logger.warning("No settlement for %s %s after 3h", asset, start)


async def serve():
    directory = data_directory()
    store = Store(directory / "paper.sqlite3")
    path = directory / "health.json"
    stop = asyncio.Event()
    for signum in (os_signal.SIGTERM, os_signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    headers = {"User-Agent": "nfl-fair-value-paper/1"}
    async with httpx.AsyncClient(base_url=BASE, timeout=10, headers=headers) as pm, \
            httpx.AsyncClient(timeout=8, headers=headers) as cb:
        worker = Worker(store, Feed(ReadOnlyVenueClient(pm, min_interval=0.2), cb))
        while not stop.is_set():
            try:
                await worker.tick()
            except Exception:
                logger.exception("Up/Down worker cycle failed")
                worker.errors["cycle"] = "unexpected error (see logs)"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                                       "errors": worker.errors, "assets": list(worker.settings.assets)}))
            tmp.replace(path)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
