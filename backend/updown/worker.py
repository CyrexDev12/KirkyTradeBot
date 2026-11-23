"""PAPER_ONLY Up/Down worker: every few seconds, price each open window, simulate
entries/early exits against the live book, and settle finished windows.

Run: ``python -m backend.updown.worker`` (crypto, ``data/updown``) or
``python -m backend.gold.worker`` (gold, ``data/gold``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import signal as os_signal
import time

import httpx

from ..trading.venue import BASE, ReadOnlyVenueClient
from .feed import Feed, FeedError, closing_minute_average, point_price
from .model import fair_value, sigma_per_second
from .store import Store, data_directory
from .strategy import SETTINGS, Settings, bracket_exit, early_exit, evaluate

logger = logging.getLogger(__name__)
FALLBACK_AFTER = 1800  # seconds after window end before using the index-derived close
ERRORS = (httpx.HTTPError, FeedError, ValueError, KeyError)


class Worker:
    """One paper account. Optional hooks let a desk (WTI) plug in its own pieces:

    * ``fair_fn(product, window, now, index_price, sigma, closing_avg, seconds, minutes, book)``
      returns P(Up) in place of the built-in model (raise ValueError when it can't price);
    * ``gate(product, window, now)`` returns a reason to hold off new entries (scheduled news,
      a contract roll, a price shock), or None;
    * ``pre_exit(trade, book, now, fair_up)`` returns a sale (like ``early_exit``) to get out of
      an open bet ahead of scheduled news, or None.
    """

    def __init__(self, store: Store, feed: Feed, settings: Settings = SETTINGS, clock=time.time,
                 fair_fn=None, gate=None, pre_exit=None, tag: dict | None = None):
        self.store, self.feed, self.settings, self.clock = store, feed, settings, clock
        self.fair_fn, self.gate, self.pre_exit, self.tag = fair_fn, gate, pre_exit, tag or {}
        self.products = {p.key: p for p in settings.products}
        self.windows: dict[tuple, object] = {}
        self.errors: dict[str, str] = {}
        self._checked: dict[tuple, float] = {}
        self._missing: dict[tuple, float] = {}

    async def _window(self, product, start):
        key = (product.key, start)
        cached = self.windows.get(key)
        if cached is None or cached.price_to_beat is None:
            cached = await self.feed.window(product, start)
            self.windows[key] = cached
            for old in [k for k in self.windows if k[1] < start - 2 * product.seconds and k[0] == product.key]:
                self.windows.pop(old)
        return cached

    async def tick(self):
        ids = list(dict.fromkeys(p.index_id for p in self.products.values()))
        try:
            seconds = await self.feed.seconds(ids)
            minutes = await self.feed.minutes(ids)
        except ERRORS as exc:
            self.errors["index"] = f"{type(exc).__name__}: {exc}"[:200]
            return
        self.errors.pop("index", None)
        for trade in self.store.open_trades():
            await self._manage(trade, seconds, minutes)
        for product in self.products.values():
            start = product.current_start(self.clock())
            # A series that isn't listed (e.g. gold 5m, or gold on weekends) is re-checked once a minute.
            if self.clock() - self._missing.get((product.key, start), -1e9) < 60:
                continue
            try:
                await self._consider(product, start, seconds.get(product.symbol), minutes.get(product.symbol))
                self.errors.pop(product.key, None)
            except ERRORS as exc:
                self.errors[product.key] = f"{type(exc).__name__}: {exc}"[:200]
                if "no " in str(exc) and "market listed" in str(exc):
                    self._missing[(product.key, start)] = self.clock()
                    self.store.note_window(product.key, start, "no market listed for this window", self.clock())
                else:
                    self.store.note_window(product.key, start, f"data unavailable: {type(exc).__name__}", self.clock())
        await self._resolve_windows()

    def _inputs(self, window, seconds, minutes, now):
        if not seconds or not minutes:
            raise FeedError("missing index prices")
        if now - seconds[-1][0] > self.settings.max_index_age:
            raise FeedError("stale settlement-index price")
        sigma = sigma_per_second([c for _, c in minutes[-121:]])
        avg = (closing_minute_average(seconds, window.end, now)
               if window.rule == "avg60" and window.end - now <= 60 else None)
        return seconds[-1][1], sigma, avg

    async def _consider(self, product, start, seconds, minutes):
        window = await self._window(product, start)
        now = self.clock()
        key = product.key
        held = self.store.held(key, start)
        if held or window.price_to_beat is None or now < window.start + self.settings.entry_after_seconds - 5:
            if not held:
                why = ("market not open" if window.status != "MARKET_STATUS_OPEN"
                       else "waiting for the price to beat / entry part")
                self.store.note_window(key, start, why, now, window.price_to_beat)
            return
        if window.status != "MARKET_STATUS_OPEN":
            self.store.note_window(key, start, f"market not open ({window.status})", now, window.price_to_beat)
            return
        book = await self.feed.book(window.slug)
        coinbase = None
        if product.coinbase:
            try:
                coinbase = await self.feed.coinbase(product.coinbase)
            except ERRORS as exc:
                self.errors[f"coinbase-{product.coinbase}"] = f"{type(exc).__name__}: {exc}"[:200]
        now = self.clock()
        index_price, sigma, avg = self._inputs(window, seconds, minutes, now)
        p_up = self._fair(product, window, now, index_price, sigma, avg, seconds, minutes, book)
        snap = self.store.snapshot(now)
        decision = evaluate(window, book, now, index_price, sigma, avg, coinbase, snap["balance"],
                            snap["open_stake"], snap["today_pnl"], held, self.settings, p_up=p_up,
                            cross_check=bool(product.coinbase))
        held_off = self.gate(product, window, now) if self.gate else None
        if held_off and decision.action == "BUY":
            decision.action, decision.reason = "SKIP", held_off
        self.store.observe(key, start, now, seconds_left=window.end - now, index_price=index_price,
                           coinbase=coinbase, fair_up=decision.fair_up,
                           yes_bid=book.yes_bids[0][0] if book.yes_bids else None,
                           yes_ask=book.yes_offers[0][0] if book.yes_offers else None)
        if decision.action == "BUY":
            trade = self.store.open(window, decision, now, {
                "index_price": index_price, "coinbase_price": coinbase, "coinbase_product": product.coinbase,
                "sigma_per_second": sigma, "seconds_left": round(window.end - now, 1), "rule": window.rule,
                **self.tag})
            if trade:
                logger.info("PAPER BUY %s %s x%s @ %.3f", key, decision.side, decision.quantity, decision.price)
        self.store.note_window(key, start, decision.reason, now, window.price_to_beat)

    async def _manage(self, trade, seconds, minutes):
        product = self.products.get(trade["key"])
        if product is None:
            return
        start, end = trade["window_start_ts"], trade["window_end_ts"]
        now = self.clock()
        try:
            if now < end:
                window = await self._window(product, start)
                book = await self.feed.book(trade["market_slug"])
                bracket = bracket_exit(trade, book, self.settings)
                if bracket:  # the ticket's take-profit or stop-loss was hit
                    self.store.partial_exit(trade["id"], bracket, self.clock())
                    return
                now = self.clock()
                index_price, sigma, avg = self._inputs(window, seconds.get(product.symbol),
                                                       minutes.get(product.symbol), now)
                p_up = self._fair(product, window, now, index_price, sigma, avg, seconds.get(product.symbol),
                                  minutes.get(product.symbol), book)
                sold = (self.pre_exit(trade, book, now, p_up) if self.pre_exit else None) or \
                    early_exit(trade["side"], trade["remaining"], book, p_up, trade["theta"], self.settings)
                if sold:
                    self.store.partial_exit(trade["id"], sold, self.clock())
                return
            key = (product.key, start)
            if now - self._checked.get(key, 0) < 15:
                return
            self._checked[key] = now
            _, price = await self.feed.settlement(product, start)
            if price is not None:
                self.store.settle(trade["id"], price, self.clock())
                self.store.record_outcome(product.key, start, trade["price_to_beat"], price)
                self._checked.pop(key, None)
            elif now >= end + FALLBACK_AFTER:
                close = await self._index_close(product, end)
                if close is not None:
                    self.store.settle(trade["id"], close, self.clock(), source="index fallback")
                    self.store.record_outcome(product.key, start, trade["price_to_beat"], close)
        except ERRORS as exc:
            self.errors[f"trade-{trade['id']}"] = f"{type(exc).__name__}: {exc}"[:200]

    def _fair(self, product, window, now, index_price, sigma, avg, seconds, minutes, book):
        if self.fair_fn:
            return self.fair_fn(product, window, now, index_price, sigma, avg, seconds, minutes, book)
        return fair_value(window.rule, window.price_to_beat, index_price, window.end - now, sigma, avg)

    async def _index_close(self, product, end):
        rows = (await self.feed.seconds([product.index_id], lookback=int(self.clock() - end) + 70)).get(product.symbol, [])
        if product.rule == "point":
            price = point_price(rows, end)
            return round(price, product.decimals) if price is not None else None
        vals = [p for t, p in rows if end - 60 <= t < end]
        return round(sum(vals) / len(vals), 2) if len(vals) >= 50 else None

    async def _resolve_windows(self):
        """Record outcomes of windows the bot watched but did not trade (for calibration)."""
        now = self.clock()
        for key, start, ptb in self.store.unresolved_windows(int(now) - 60):
            product = self.products.get(key)
            if product is None or ptb is None or start + product.seconds > now - 60:
                continue
            if now - self._checked.get((key, start), 0) < 60:
                continue
            self._checked[(key, start)] = now
            try:
                _, price = await self.feed.settlement(product, start)
            except ERRORS:
                continue
            if price is not None:
                self.store.record_outcome(key, start, ptb, price)
                self._checked.pop((key, start), None)
            elif now > start + product.seconds + 3 * 3600:
                self.store.record_outcome(key, start, ptb, None)  # unknown; stop polling
                logger.warning("No settlement for %s %s after 3h", key, start)


async def serve(directory: Path | None = None, settings: Settings = SETTINGS, name: str = "Up/Down"):
    directory = directory or data_directory()
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
        worker = Worker(store, Feed(ReadOnlyVenueClient(pm, min_interval=0.2), cb), settings)
        while not stop.is_set():
            try:
                await worker.tick()
            except Exception:
                logger.exception("%s worker cycle failed", name)
                worker.errors["cycle"] = "unexpected error (see logs)"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                                       "errors": worker.errors, "markets": list(worker.products)}))
            tmp.replace(path)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
