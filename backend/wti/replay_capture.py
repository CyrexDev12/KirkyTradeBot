"""Read-only WTI market recording on the Azure worker."""

import asyncio
from base64 import b64decode, b64encode
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
import json
import logging
import os
import sqlite3
from threading import Lock
import time

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ..trading.venue import public_request_scope
from ..updown.feed import FeedError, parse_book
from ..updown.strategy import Book
from .live_books import ENTRY_EXCHANGE_MAX_AGE, usable
from .shadow import StreamQuotes, decode_window
from .tape import Tape, tape_path

logger = logging.getLogger(__name__)
WS_PATH = "/v1/ws/markets"
WS_URL = "wss://api.polymarket.us" + WS_PATH
SHADOW_HEALTH_FILE = "shadow-health.json"
SHADOW_POLL_SECONDS = 2
STREAM_BATCH_ROWS = 128
STREAM_BUFFER_ROWS = 1024
NON_OPEN_MARKET_STATES = frozenset(f"MARKET_STATE_{state}" for state in (
    "PREOPEN", "SUSPENDED", "HALTED", "EXPIRED", "TERMINATED", "MATCH_AND_CLOSE_AUCTION", "CLOSED",
))


def stream_headers(key_id, key, now):
    timestamp = str(int(now * 1000))
    signature = key.sign(f"{timestamp}GET{WS_PATH}".encode())
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": timestamp,
            "X-PM-Signature": b64encode(signature).decode("ascii")}


def venue_timestamp(timestamp):
    try:
        stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
        return stamp.timestamp() if stamp and stamp.tzinfo else None
    except (AttributeError, ValueError, OverflowError, OSError):
        return None


def stream_row(message, slug, now):
    """Normalize the full public book or trade frame, retaining receive and venue time."""
    raw = message.get("marketData") or message.get("market_data")
    if isinstance(raw, dict) and (raw.get("marketSlug") or raw.get("market_slug")) == slug:
        timestamp = raw.get("transactTime") or raw.get("transact_time")
        state = raw.get("state")
        if isinstance(state, str) and state in NON_OPEN_MARKET_STATES:
            return slug, now, "stream", "closed", {"exchange_at": venue_timestamp(timestamp)}
        if not all(isinstance(raw.get(side), list) for side in ("bids", "offers")):
            return slug, now, "stream", "gap", {}
        data = {**raw, "transactTime": timestamp,
                "sequence": raw.get("sequence", raw.get("seqNum", raw.get("seq_num")))}
        try:
            book = parse_book({"marketData": data}, now, slug=slug)
        except FeedError:
            return slug, now, "stream", "gap", {}
        return slug, now, "stream", "book", asdict(book)
    trade = message.get("trade")
    if isinstance(trade, dict) and (trade.get("marketSlug") or trade.get("market_slug")) == slug:
        timestamp = trade.get("tradeTime") or trade.get("trade_time")
        data = {"price": trade.get("price"), "quantity": trade.get("quantity"),
                "exchange_at": venue_timestamp(timestamp)}
        return slug, now, "stream", "trade", data
    return None


class Capture:
    """Observe a single WTI 15-minute contract at a time, shared by all models."""

    def __init__(self, directory, feed, clock=time.time):
        self.tape = Tape(tape_path(directory))
        self.feed, self.clock = feed, clock
        self.tasks = {}
        self.latest_window = None
        self.stream = MarketStream(self.tape, clock)

    @property
    def status(self):
        return self.stream.health()["state"] if self.stream.configured else "rest"

    def _write(self, method, *args):
        try:
            method(*args)
        except (OSError, sqlite3.Error, ValueError):
            logger.warning("WTI replay write failed", exc_info=True)
            return False
        return True

    def window(self, window):
        if window.key != "wti-15m" or not window.start <= self.clock() < window.end:
            return
        if self.latest_window != window and self._write(self.tape.record_window, window):
            self.latest_window = window
        if window.slug in self.tasks:
            return
        self.tasks = {slug: task for slug, task in self.tasks.items() if not task.done()}
        self.tasks[window.slug] = asyncio.create_task(self._capture(window))

    def intent(self, model, key, slug, at, payload):
        if key == "wti-15m":
            self._write(self.tape.append, slug, at, "intent", "order", {"model": model, **payload})

    def settlement(self, result):
        if result is not None:
            self._write(self.tape.record_settlement, result, self.clock())

    async def _capture(self, window):
        tasks = [asyncio.create_task(self._snapshots(window))]
        if self.stream.configured:
            tasks.append(asyncio.create_task(self.stream.run(window)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _snapshots(self, window):
        while self.clock() < window.end:
            try:
                with public_request_scope("wti_signal"):
                    book = await asyncio.wait_for(self.feed.book(window.slug), timeout=4)
            except (TimeoutError, httpx.HTTPError, ValueError, KeyError) as exc:
                kind = "closed" if isinstance(exc, FeedError) and str(exc).startswith("book MARKET_STATE_") else "gap"
                self._write(self.tape.append, window.slug, self.clock(), "rest", kind, {})
            else:
                self._write(self.tape.append, window.slug, book.at, "rest", "book", asdict(book))
            await asyncio.sleep(2)

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


class StreamBookSource:
    """Thread-safe view of the current socket. A confirmed halt survives reconnect."""

    def __init__(self, clock=time.time):
        self._clock, self._lock = clock, Lock()
        self._window = None
        self._generation = 0
        self._connected = False
        self._quotes = StreamQuotes(exchange_max_age=ENTRY_EXCHANGE_MAX_AGE)
        self._last_accepted = None
        self._last_frame_at = float("-inf")
        self._non_open = False
        self._closed_exchange_at = float("-inf")

    def begin(self, window):
        with self._lock:
            current = ((window.slug, window.start, window.end)
                       if window.key == "wti-15m" and window.end - window.start == 900 else None)
            if current != self._window:
                self._window = current
                self._quotes = StreamQuotes(exchange_max_age=ENTRY_EXCHANGE_MAX_AGE)
                self._last_accepted = None
                self._last_frame_at = self._closed_exchange_at = float("-inf")
                self._non_open = False
            self._generation += 1
            self._connected = False
            self._quotes.quote = None
            return self._generation

    def set_connected(self, generation, connected):
        with self._lock:
            if generation == self._generation:
                self._connected = connected
                if not connected:
                    self._quotes.quote = None

    def snapshot(self, slug: str) -> tuple[Book | None, bool]:
        with self._lock:
            now = self._clock()
            if self._window is None or not (self._window[0] == slug and self._window[1] <= now < self._window[2]):
                return None, False
            quote = self._quotes.quote if self._connected and not self._non_open else None
            if quote and usable(quote, now, exchange_max_age=ENTRY_EXCHANGE_MAX_AGE):
                return quote, False
            return None, self._non_open

    def observe(self, generation, row):
        slug, at, source, kind, payload = row
        with self._lock:
            if (generation != self._generation or not self._connected or self._window is None
                    or source != "stream" or slug != self._window[0] or not self._window[1] <= at < self._window[2]):
                return
            if kind == "closed":
                self._non_open = True
                stamp = payload.get("exchange_at")
                if stamp is None or stamp > at + 2:
                    # With no trustworthy state timestamp, wait for an exchange update after receipt.
                    stamp = at
                self._closed_exchange_at = max(self._closed_exchange_at, stamp)
                if self._quotes.exchange_at is not None:
                    self._closed_exchange_at = max(self._closed_exchange_at, self._quotes.exchange_at)
            if at < self._last_frame_at:
                self._quotes.quote = None
                return
            self._last_frame_at = at
            if kind == "book" and self._repeated_or_before_close(payload):
                return
            accepted = self._quotes.update(at, kind, payload)
            if accepted is not None:
                self._last_accepted = accepted
                self._non_open = False

    def _repeated_or_before_close(self, payload):
        stamp = payload.get("exchange_at")
        if self._non_open and (stamp is None or stamp <= self._closed_exchange_at):
            self._quotes.quote = None
            return True
        last = self._last_accepted
        sequence = payload.get("sequence")
        if (last is None or stamp != last.exchange_at
                or (last.sequence is not None and sequence is not None and sequence > last.sequence)):
            return False
        # The same venue update cannot acquire a later receipt time from a repeat delivery.
        if ((payload.get("yes_bids"), payload.get("yes_offers"), sequence)
                != (last.yes_bids, last.yes_offers, last.sequence)):
            self._quotes.quote = None
        return True


class MarketStream:
    def __init__(self, tape, clock, credentials=None, *, quote_source=None):
        self.tape, self.clock = tape, clock
        self.quote_source = quote_source
        replay_key = os.getenv("WTI_REPLAY_KEY_ID")
        replay_secret = os.getenv("WTI_REPLAY_SECRET_KEY")
        self.key_id, secret = ((replay_key, replay_secret) if replay_key or replay_secret
                               else credentials or (None, None))
        self.key = None
        self.state = "disabled"
        self.window = None
        self.quotes = StreamQuotes()
        if self.key_id and secret:
            try:
                self.key = Ed25519PrivateKey.from_private_bytes(b64decode(secret, validate=True)[:32])
                self.state = "waiting_for_market"
            except ValueError:
                logger.warning("WTI shadow streaming key is invalid.")
                self.state = "invalid_credentials"
        elif self.key_id or secret:
            logger.warning("WTI shadow streaming needs both the key ID and secret.")
            self.state = "invalid_credentials"

    @property
    def configured(self):
        return self.key is not None

    def health(self):
        now = self.clock()
        current = self.window if self.window and self.window.start <= now < self.window.end else None
        latest = self.quotes.last_book if current else None
        state = self.state
        if self.configured and current is None:
            state = "waiting_for_market"
        elif state == "connected":
            if self.quotes.quote and usable(self.quotes.quote, now):
                state = "fresh"
            else:
                state = "no_fresh_book"
        return {"state": state, "mode": "shadow", "source": "stream", "slug": current.slug if current else None,
                "last_book_received_at": latest.at if latest else None,
                "last_book_exchange_at": latest.exchange_at if latest else None, "checked_at": now}

    async def _write(self, method, *args):
        try:
            await asyncio.to_thread(method, *args)
        except (OSError, sqlite3.Error, ValueError):
            logger.warning("WTI shadow tape write failed", exc_info=True)

    async def run(self, window):
        self.window = window
        self.quotes = StreamQuotes()
        backoff = 2
        if self.clock() < window.end:
            await self._write(self.tape.append, window.slug, self.clock(), "stream", "gap", {})
        while window.start <= self.clock() < window.end:
            generation = self.quote_source.begin(window) if self.quote_source is not None else None
            self.state = "connecting"
            try:
                async with connect(WS_URL, additional_headers=stream_headers(self.key_id, self.key, self.clock()),
                                   open_timeout=5, close_timeout=2) as socket:
                    for kind in ("MARKET_DATA", "TRADE"):
                        await socket.send(json.dumps({"subscribe": {"requestId": kind,
                            "subscriptionType": f"SUBSCRIPTION_TYPE_{kind}", "marketSlugs": [window.slug]}}))
                    self.state = "connected"
                    if self.quote_source is not None:
                        self.quote_source.set_connected(generation, True)
                    await self._stream_messages(socket, window, generation)
            except (TimeoutError, OSError, ValueError, WebSocketException):
                self.state = "unavailable"
                self.quotes.update(self.clock(), "gap", {})
                if self.clock() < window.end:
                    await self._write(self.tape.append, window.slug, self.clock(), "stream", "gap", {})
                # Avoid logging handshake headers or remote error messages that may contain credentials.
                logger.warning("WTI shadow market stream interrupted. REST capture continues.")
                await asyncio.sleep(min(backoff, max(0, window.end - self.clock())))
                backoff = min(backoff * 2, 60)

    async def _stream_messages(self, socket, window, generation=None):
        rows = []
        writer = None
        flushed_at = self.clock()
        last_message_at = flushed_at
        try:
            while self.clock() < window.end:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=1)
                except TimeoutError:
                    if self.clock() - last_message_at >= 20:
                        raise
                else:
                    last_message_at = self.clock()
                    if last_message_at >= window.end:
                        break
                    message = json.loads(raw)
                    if not isinstance(message, dict) or message.get("error"):
                        raise ValueError("market subscription rejected")
                    row = stream_row(message, window.slug, last_message_at)
                    if row:
                        if len(rows) >= STREAM_BUFFER_ROWS:
                            # Mark the first missing observation so replay cannot carry an old book across the loss.
                            rows = [(window.slug, rows[0][1], "stream", "gap", {"reason": "tape_buffer_overflow"})]
                        rows.append(row)
                        self.quotes.update(row[1], row[3], row[4])
                        if self.quote_source is not None:
                            self.quote_source.observe(generation, row)
                if writer is not None and writer.done():
                    writer.result()
                    writer = None
                if rows and writer is None and (len(rows) >= STREAM_BATCH_ROWS or self.clock() - flushed_at >= 1):
                    batch, rows = rows, []
                    writer = asyncio.create_task(self._write(self.tape.append_many, batch))
                    flushed_at = self.clock()
        finally:
            if self.quote_source is not None:
                self.quote_source.set_connected(generation, False)
            if writer is not None:
                await writer
            if rows:
                await self._write(self.tape.append_many, tuple(rows))


def current_window(path, now):
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            rows = db.execute(
                "SELECT slug,start,end,window FROM markets WHERE start BETWEEN ? AND ? AND end>? AND end-start=900 "
                "ORDER BY start DESC,slug", (now - 900, now, now),
            )
            return next((window for row in rows if (window := decode_window(row)) is not None), None)
    return None


def write_health(path, status):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status), encoding="utf-8")
    temporary.replace(path)


async def run_shadow_stream(directory, stop, *, credentials=None, clock=time.time, stream_factory=MarketStream,
                            quote_source=None):
    """Record one market socket and optionally share its in-memory quote state."""
    path = tape_path(directory)
    stream = task = active = None
    tape_unavailable = False
    try:
        while not stop.is_set():
            try:
                if stream is None:
                    tape = await asyncio.to_thread(Tape, path)
                    options = {"quote_source": quote_source} if quote_source is not None else {}
                    stream = stream_factory(tape, clock, credentials, **options)
                current = await asyncio.to_thread(current_window, path, clock()) if stream.configured else None
                target = (current.slug, current.end) if current else None
                if target != active:
                    if task:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    task = asyncio.create_task(stream.run(current)) if current else None
                    active = target
                if task and task.done():
                    finished, task, active = task, None, None
                    finished.result()
                await asyncio.to_thread(write_health, path.with_name(SHADOW_HEALTH_FILE), stream.health())
                tape_unavailable = False
            except (OSError, sqlite3.Error, ValueError):
                if not tape_unavailable:
                    logger.warning("WTI shadow observer could not use the tape.", exc_info=True)
                tape_unavailable = True
            try:
                await asyncio.wait_for(stop.wait(), timeout=SHADOW_POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
