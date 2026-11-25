"""PAPER_ONLY Up/Down worker: every few seconds, price each open window, simulate
entries/early exits against the live book, and settle finished windows.

Run: ``python -m backend.updown.worker`` (crypto, ``data/updown``) or
``python -m backend.gold.worker`` (gold, ``data/gold``).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import signal as os_signal
import time

import httpx

from ..trading.venue import BASE, ReadOnlyVenueClient
from .feed import Feed, FeedError, closing_minute_average
from .model import fair_value, sigma_per_second
from .store import Store, data_directory
from .strategy import SETTINGS, Decision, Settings, Window, bracket_exit, early_exit, evaluate, match_entry, match_exit

logger = logging.getLogger(__name__)
ERRORS = (httpx.HTTPError, FeedError, ValueError, KeyError)


@dataclass(frozen=True)
class EntryIntent:
    window: Window
    decision: Decision
    decided_at: float
    extra: dict


@dataclass(frozen=True)
class ExitIntent:
    decision: dict
    decided_at: float


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
                 fair_fn=None, gate=None, pre_exit=None, tag: dict | None = None, on_intent=None):
        self.store, self.feed, self.settings, self.clock = store, feed, settings, clock
        self.fair_fn, self.gate, self.pre_exit, self.tag = fair_fn, gate, pre_exit, tag or {}
        self.on_intent = on_intent
        self.products = {p.key: p for p in settings.products}
        self.windows: dict[tuple, object] = {}
        self.errors: dict[str, str] = {}
        self._checked: dict[tuple, float] = {}
        self._missing: dict[tuple, float] = {}
        self._entries: dict[str, EntryIntent] = {}
        self._exits: dict[int, ExitIntent] = {}

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
        open_trades = self.store.open_trades()
        now = self.clock()
        for trade in open_trades:
            if trade["window_end_ts"] <= now:
                await self._manage(trade, {}, {})
        ids = list(dict.fromkeys(p.index_id for p in self.products.values()))
        index_ready = False
        try:
            seconds = await self.feed.seconds(ids)
            minutes = await self.feed.minutes(ids)
        except ERRORS as exc:
            self.errors["index"] = f"{type(exc).__name__}: {exc}"[:200]
            seconds, minutes = {}, {}
            self._entries.clear()
        else:
            self.errors.pop("index", None)
            index_ready = True
        for trade in open_trades:
            if trade["window_end_ts"] > now:
                await self._manage(trade, seconds, minutes)
        if index_ready:
            await self._scan_entries(seconds, minutes)
        await self._resolve_windows()

    async def _scan_entries(self, seconds, minutes):
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
        key = product.key
        held = self.store.held(key, start)
        pending = self._entries.get(key)
        if pending and (held or pending.window.start != start):
            self._entries.pop(key)
            pending = None
        if held:
            return
        if pending:
            ready_at = pending.decided_at + self.settings.execution_latency_seconds
            if self.clock() >= ready_at:
                self._entries.pop(key)
                await self._execute_entry(product, pending, ready_at)
            return
        window = await self._window(product, start)
        now = self.clock()
        if window.price_to_beat is None or now < window.start + self.settings.entry_after_seconds - 5:
            why = ("market not open" if window.status != "MARKET_STATUS_OPEN"
                   else "waiting for the price to beat / entry part")
            self.store.note_window(key, start, why, now, window.price_to_beat)
            return
        if window.status != "MARKET_STATUS_OPEN":
            self.store.note_window(key, start, f"market not open ({window.status})", now, window.price_to_beat)
            return
        if now >= window.end - self.settings.entry_before_end_seconds:
            self.store.note_window(key, start, "entry window closed", now, window.price_to_beat)
            return
        book = await self.feed.book(window.slug)
        coinbase = None
        if product.coinbase:
            try:
                coinbase = await self.feed.coinbase(product.coinbase)
            except ERRORS as exc:
                self.errors[f"coinbase-{product.coinbase}"] = f"{type(exc).__name__}: {exc}"[:200]
        now = self.clock()
        self._check_book(book, now)
        available = self.store.execution_book(window.slug, book)
        if available is None:
            raise FeedError("book observation arrived out of order")
        index_price, sigma, avg = self._inputs(window, seconds, minutes, now)
        p_up = self._fair(product, window, now, index_price, sigma, avg, seconds, minutes, book)
        snap = self.store.snapshot(now)
        decision = evaluate(window, available, now, index_price, sigma, avg, coinbase, snap["balance"],
                            snap["open_stake"], snap["today_pnl"], held, self.settings, p_up=p_up,
                            cross_check=bool(product.coinbase))
        held_off = self.gate(product, window, now) if self.gate else None
        if held_off and decision.action == "BUY":
            decision.action, decision.reason = "SKIP", held_off
        self.store.observe(key, start, now, seconds_left=window.end - now, index_price=index_price,
                           coinbase=coinbase, fair_up=decision.fair_up,
                           yes_bid=book.yes_bids[0][0] if book.yes_bids else None,
                           yes_ask=book.yes_offers[0][0] if book.yes_offers else None)
        reason = decision.reason
        if decision.action == "BUY":
            decided_at = self.clock()
            self._entries[key] = EntryIntent(window, decision, decided_at, {
                "index_price": index_price, "coinbase_price": coinbase, "coinbase_product": product.coinbase,
                "sigma_per_second": sigma, "rule": window.rule,
                **self.tag})
            if self.on_intent:
                self.on_intent(key, window.slug, decided_at, {
                    "action": "buy", "side": decision.side, "quantity": decision.quantity,
                    "limit": decision.fills[-1][0], "budget": decision.cost, "fair_up": decision.fair_up,
                    "min_edge": self.settings.min_edge, "theta": window.theta,
                    "ready_at": decided_at + self.settings.execution_latency_seconds,
                    "deadline": window.end - self.settings.entry_before_end_seconds,
                })
            reason = "buy signaled; waiting for an execution quote"
        self.store.note_window(key, start, reason, now, window.price_to_beat)

    def _check_book(self, book, now):
        if not 0 <= now - book.at <= self.settings.max_book_age_seconds:
            raise FeedError("book receipt is stale or ahead of the worker clock")
        if book.exchange_at is not None and book.exchange_at > now + 2:
            raise FeedError("exchange book timestamp is ahead of the worker clock")

    async def _arrival_book(self, slug, ready_at, deadline):
        if self.clock() >= deadline:
            return None
        read_for_execution = getattr(self.feed, "execution_book", None)
        book = await (read_for_execution(slug, ready_at) if read_for_execution else self.feed.book(slug))
        now = self.clock()
        if now >= deadline or (book.exchange_at is not None and book.exchange_at >= deadline):
            return None
        self._check_book(book, now)
        requested = book.requested_at if book.requested_at is not None else book.at
        if not ready_at <= requested <= book.at:
            raise FeedError("book was requested before the order could arrive")
        return book

    async def _execute_entry(self, product, intent, ready_at):
        window, signal = intent.window, intent.decision
        deadline = window.end - self.settings.entry_before_end_seconds
        book = await self._arrival_book(window.slug, ready_at, deadline)
        now = self.clock()
        reason = "entry expired before execution" if book is None else "no fill at the signaled limit"
        if book is not None:
            available = self.store.execution_book(window.slug, book)
            if available is None:
                raise FeedError("execution book arrived out of order")
            held_off = self.gate(product, window, now) if self.gate else None
            snap = self.store.snapshot(now)
            if held_off:
                reason = held_off
            elif snap["today_pnl"] <= -self.settings.daily_loss_limit * (snap["balance"] - snap["today_pnl"]):
                reason = "daily loss limit reached before execution"
            else:
                budget = min(signal.cost, snap["balance"] - snap["open_stake"],
                             snap["balance"] * self.settings.max_open_stake - snap["open_stake"],
                             snap["balance"] * self.settings.max_stake)
                fill = match_entry(window, available, signal.side, signal.quantity, signal.fills[-1][0],
                                   signal.fair_up, budget, self.settings, balance=snap["balance"],
                                   open_stake=snap["open_stake"])
                now = self.clock()
                if fill and now < deadline:
                    trade = self.store.open(window, fill, now, {
                        **intent.extra, "seconds_left": round(window.end - now, 1),
                        "signal_at": intent.decided_at, "order_ready_at": ready_at,
                    }, book=book, participation=self.settings.participation)
                    if trade:
                        reason = fill.reason
                        logger.info("PAPER BUY %s %s x%s @ %.3f", product.key, fill.side, fill.quantity, fill.price)
        self.store.note_window(product.key, window.start, reason, now, window.price_to_beat)

    async def _manage(self, trade, seconds, minutes):
        product = self.products.get(trade["key"])
        if product is None:
            return
        try:
            if self.clock() < trade["window_end_ts"]:
                await self._manage_exit(trade, product, seconds.get(product.symbol), minutes.get(product.symbol))
            if self.clock() >= trade["window_end_ts"]:
                self._exits.pop(trade["id"], None)
                await self._settle_trade(trade, product)
        except ERRORS as exc:
            self.errors[f"trade-{trade['id']}"] = f"{type(exc).__name__}: {exc}"[:200]

    async def _manage_exit(self, trade, product, seconds, minutes):
        pending = self._exits.get(trade["id"])
        if pending:
            ready_at = pending.decided_at + self.settings.execution_latency_seconds
            if self.clock() >= ready_at:
                self._exits.pop(trade["id"])
                await self._execute_exit(trade, pending, ready_at)
            return
        book = await self.feed.book(trade["market_slug"])
        now = self.clock()
        if now >= trade["window_end_ts"]:
            return
        self._check_book(book, now)
        available = self.store.execution_book(trade["market_slug"], book)
        if available is None:
            return
        decision = bracket_exit(trade, available, self.settings)
        if decision is None and seconds and minutes:
            window = await self._window(product, trade["window_start_ts"])
            now = self.clock()
            if now >= trade["window_end_ts"]:
                return
            self._check_book(book, now)
            index_price, sigma, avg = self._inputs(window, seconds, minutes, now)
            p_up = self._fair(product, window, now, index_price, sigma, avg, seconds, minutes, book)
            decision = (self.pre_exit(trade, available, now, p_up) if self.pre_exit else None) or \
                early_exit(trade["side"], trade["remaining"], available, p_up, trade["theta"], self.settings)
        if decision:
            decided_at = self.clock()
            self._exits[trade["id"]] = ExitIntent(decision, decided_at)
            if self.on_intent:
                kind, quantity, limit = self._exit_terms(trade, decision)
                self.on_intent(trade["key"], trade["market_slug"], decided_at, {
                    "action": "sell", "side": trade["side"], "quantity": quantity,
                    "limit": limit, "exit_kind": kind, "theta": trade["theta"],
                    "ready_at": decided_at + self.settings.execution_latency_seconds,
                    "deadline": trade["window_end_ts"],
                })

    @staticmethod
    def _exit_terms(trade, signal):
        kind = signal.get("kind", "take_profit")
        if kind == "target":
            return kind, trade["remaining"], trade["take_profit_price"]
        if kind == "stop_loss":
            return kind, trade["remaining"], 0.0
        return kind, signal["quantity"], signal["fills"][-1][0]

    async def _execute_exit(self, trade, intent, ready_at):
        end = trade["window_end_ts"]
        book = await self._arrival_book(trade["market_slug"], ready_at, end)
        if book is None:
            return
        available = self.store.execution_book(trade["market_slug"], book)
        if available is None:
            return
        signal = intent.decision
        kind, quantity, limit = self._exit_terms(trade, signal)
        fill = match_exit(trade, available, quantity, limit, kind, self.settings)
        now = self.clock()
        if fill and now < end:
            fill.update({key: signal[key] for key in ("fair_side", "trigger", "news") if key in signal})
            fill.update(signal_at=intent.decided_at, order_ready_at=ready_at)
            self.store.partial_exit(trade["id"], fill, now, book=book, participation=self.settings.participation)

    def _fair(self, product, window, now, index_price, sigma, avg, seconds, minutes, book):
        if self.fair_fn:
            return self.fair_fn(product, window, now, index_price, sigma, avg, seconds, minutes, book)
        return fair_value(window.rule, window.price_to_beat, index_price, window.end - now, sigma, avg)

    async def _settle_trade(self, trade, product):
        now = self.clock()
        key = (product.key, trade["window_start_ts"])
        if now - self._checked.get(key, float("-inf")) < 15:
            return
        self._checked[key] = now
        result = await self.feed.settlement(product, key[1])
        if result is None:
            return
        if result.slug != trade["market_slug"]:
            raise FeedError("settlement market differs from trade")
        self.store.settle(trade["id"], result.price, self.clock(), yes_payout=result.yes_payout,
                          official_price_to_beat=result.price_to_beat)
        self._checked.pop(key, None)
        self.errors.pop(f"trade-{trade['id']}", None)

    async def _resolve_windows(self):
        """Record outcomes of windows the bot watched but did not trade (for calibration)."""
        now = self.clock()
        before = {key: int(now) - product.seconds - 60 for key, product in self.products.items()}
        pending = sorted(self.store.unresolved_windows(before),
                         key=lambda row: (self._checked.get(row[:2], float("-inf")), row[1], row[0]))
        for key, start, ptb in pending[:5]:
            if now - self._checked.get((key, start), float("-inf")) < 60:
                break
            self._checked[(key, start)] = now
            try:
                result = await self.feed.settlement(self.products[key], start)
            except ERRORS:
                continue
            if result is not None:
                confirmed_open = result.price_to_beat if result.price_to_beat is not None else ptb
                self.store.record_outcome(key, start, confirmed_open, result.price, outcome=result.outcome)
                self._checked.pop((key, start), None)


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
