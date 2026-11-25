"""Prefer current WTI stream books and bound the use of REST fallback."""

import asyncio
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import httpx

from ..trading.request_budget import retry_after_seconds
from ..trading.venue import PublicRequestDeadline
from ..updown.feed import FeedError
from ..updown.strategy import Book

BOOK_MAX_AGE = 3.0
EXCHANGE_MAX_AGE = 5.0
ENTRY_EXCHANGE_MAX_AGE = 15.0
REQUEST_INTERVAL = 3.0
SIGNAL_TTL = 6.0
EXIT_BUFFER = 5.0
SUBMISSION_BUFFER = 2.0
TAPE_POLL_INTERVAL = 0.2
MAX_QUOTE_OBSERVATIONS = 64

_FRESHNESS_REASONS = {
    "receipt_in_future", "receipt_stale", "exchange_timestamp_missing", "exchange_in_future", "exchange_stale",
    "request_before_ready", "request_after_receipt",
}
_ASSESSMENT_MESSAGES = {
    "insufficient_depth": "Too little displayed liquidity at the WTI limit.",
    "budget_below_minimum": "The risk budget allowed less than the exchange’s minimum size.",
}
_STREAM_UNAVAILABLE = "No fresh WTI WebSocket book was available for this contract."


def _freshness_reason(book, now, requested_after, *, exchange_max_age=EXCHANGE_MAX_AGE):
    if book.at > now:
        return "receipt_in_future"
    if not 0 <= now - book.at <= BOOK_MAX_AGE:
        return "receipt_stale"
    if book.exchange_at is None:
        return "exchange_timestamp_missing"
    if now - book.exchange_at < -2:
        return "exchange_in_future"
    if not -2 <= now - book.exchange_at <= exchange_max_age:
        return "exchange_stale"
    if requested_after is not None:
        requested = book.requested_at if book.requested_at is not None else book.at
        if requested < requested_after:
            return "request_before_ready"
        if not requested_after <= requested <= book.at:
            return "request_after_receipt"
    return "usable"


def usable(book, now, requested_after=None, *, exchange_max_age=EXCHANGE_MAX_AGE):
    return _freshness_reason(book, now, requested_after, exchange_max_age=exchange_max_age) == "usable"


def _observation(source, now, ready_at, reason, book=None, **fields):
    received_at = book.at if book is not None else None
    exchange_at = book.exchange_at if book is not None else None
    requested_at = book.requested_at if book is not None else None
    if source == "stream" and received_at is not None and ready_at is not None:
        fields["frame_after_ready_seconds"] = received_at - ready_at
    return {"source": source, "reason": reason, "observed_at": now, "ready_at": ready_at,
            "received_at": received_at, "exchange_at": exchange_at, "requested_at": requested_at,
            "receipt_age_seconds": now - received_at if received_at is not None else None,
            "exchange_age_seconds": now - exchange_at if exchange_at is not None else None,
            "request_after_ready_seconds": requested_at - ready_at if requested_at is not None and ready_at is not None
            else None,
            **fields}


def _assessed_book(book, source, now, ready_at, observe, assess):
    reject, details = assess(book) if assess is not None else (None, {})
    if observe is not None:
        event = _observation(source, now, ready_at, reject or "usable", book)
        observe({**details, **event, "assessed": assess is not None, "assessment_rejected": reject is not None})
    return book if reject is None else None


class BookUnavailable(FeedError):
    def __init__(self, message, reason, status=None):
        super().__init__(message)
        self.reason, self.status = reason, status


class LiveBooks:
    def __init__(self, tape, feed, clock, *, stream_snapshot=None, stream_only=False):
        self.tape, self.feed, self.clock = tape, feed, clock
        self.stream_snapshot = stream_snapshot
        self.stream_only = stream_only
        self.cached = None
        self.next_request = float("-inf")
        self.last_unavailable = None
        self._direct_task = None
        self._direct_closures = {}
        self._entry_direct_attempts = {}

    def _recorded(self, slug, now):
        if self.tape.is_file():
            try:
                with closing(sqlite3.connect(self.tape.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
                    return db.execute("""SELECT at,kind,payload FROM tape WHERE slug=? AND source='rest' AND at<=?
                        ORDER BY at DESC,id DESC LIMIT 1""", (slug, now)).fetchone()
            except sqlite3.Error:
                return None  # The direct read must still pass the same freshness checks.
        return None

    def _select_local(self, slug, row, requested_after, observe, *, exchange_max_age, assess=None):
        now = self.clock()
        choices = []
        closed_at = self._direct_closures.get(slug, float("-inf"))
        if row and row[0] > closed_at:
            snapshot = None
            reason = {"gap": "recorded_gap", "closed": "market_not_open"}.get(row[1], "recorded_invalid")
            if row[1] == "book":
                try:
                    decoded = Book(**json.loads(row[2]))
                    if decoded.at == row[0]:
                        snapshot = decoded
                except (TypeError, ValueError):
                    pass
            if snapshot is not None:
                choices.append(("recorded_rest", snapshot))
            elif observe is not None:
                observe(_observation("recorded_rest", now, requested_after, reason, tape_at=row[0]))
        elif observe is not None:
            if now - closed_at <= BOOK_MAX_AGE:
                observe(_observation("direct_rest", now, requested_after, "market_not_open", state_at=closed_at))
            else:
                observe(_observation("recorded_rest", now, requested_after, "recorded_missing"))
        if self.cached and self.cached[0] == slug:
            cached = self.cached[1]
            if cached.at > closed_at and (not row or cached.at > row[0]):
                choices.append(("direct_rest", cached))
        for source, book in sorted(choices, key=lambda candidate: candidate[1].at, reverse=True):
            reason = _freshness_reason(book, now, requested_after, exchange_max_age=exchange_max_age)
            if reason == "usable":
                return _assessed_book(book, source, now, requested_after, observe, assess), True
            if observe is not None:
                observe(_observation(source, now, requested_after, reason, book))
        return None, False

    def _select_stream(self, slug, row, ready_at, observe, *, exchange_max_age, assess=None):
        """Return the selected book and whether the stream decided this probe."""
        if self.stream_snapshot is None and not self.stream_only:
            return None, False
        book, non_open = self.stream_snapshot(slug) if self.stream_snapshot is not None else (None, False)
        now = self.clock()
        if non_open or book is None:
            if observe is not None:
                observe(_observation("stream", now, ready_at, "market_not_open" if non_open else "stream_unavailable"))
            return None, non_open

        # A WS frame has an arrival time, but no per-book GET dispatch time.
        if book.requested_at is not None:
            book = replace(book, requested_at=None)
        reason = _freshness_reason(book, now, ready_at, exchange_max_age=exchange_max_age)
        if reason == "usable" and not self.stream_only:
            closed_at = self._direct_closures.get(slug, float("-inf"))
            if row and row[1] == "closed":
                closed_at = max(closed_at, row[0])
            if min(book.at, book.exchange_at) <= closed_at:
                reason = "market_not_open"
        if reason == "usable":
            return _assessed_book(book, "stream", now, ready_at, observe, assess), True
        if observe is not None:
            observe(_observation("stream", now, ready_at, reason, book))
        return None, False

    def _select_entry(self, slug, row, ready_at, observe, assess):
        book, stream_decides = self._select_stream(slug, row, ready_at, observe,
                                                    exchange_max_age=ENTRY_EXCHANGE_MAX_AGE, assess=assess)
        if stream_decides or self.stream_only:
            return book, False
        book, have_local_quote = self._select_local(slug, row, ready_at, observe,
                                                    exchange_max_age=ENTRY_EXCHANGE_MAX_AGE, assess=assess)
        return book, not have_local_quote

    def entry_snapshot(self, slug, ready_at, observe=None, *, assess=None):
        """Synchronously select an eligible stream or local REST book without a request."""
        row = None if self.stream_only else self._recorded(slug, self.clock())
        return self._select_entry(slug, row, ready_at, observe, assess)[0]

    def exit_snapshot(self, slug, requested_after=None):
        """Synchronously check the current exit book and raise on a confirmed stream closure."""
        row = None if self.stream_only else self._recorded(slug, self.clock())
        book, stream_decides = self._select_stream(slug, row, requested_after, None,
                                                    exchange_max_age=EXCHANGE_MAX_AGE)
        if stream_decides:
            if book is None:
                raise BookUnavailable("The WTI contract order book was not open.", "market_not_open")
            return book
        if self.stream_only:
            return None
        return self._select_local(slug, row, requested_after, None, exchange_max_age=EXCHANGE_MAX_AGE)[0]

    def _start_direct(self, slug):
        now = self.clock()
        if now < self.next_request or (self._direct_task is not None and not self._direct_task.done()):
            return None
        self.next_request = now + REQUEST_INTERVAL
        self._direct_task = asyncio.create_task(self._fetch_direct(slug))
        return self._direct_task

    async def _fetch_direct(self, slug):
        try:
            book = await self.feed.book(slug)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429:
                now = self.clock()
                delay = retry_after_seconds(exc.response.headers.get("retry-after"), now) or 5.0
                self.next_request = max(self.next_request, now + max(REQUEST_INTERVAL, delay))
                self.last_unavailable = "Polymarket US rate-limited the order book (HTTP 429)."
                reason = "rate_limited"
            else:
                self.last_unavailable = f"The Polymarket US order book returned HTTP {code}."
                reason = "http_error"
            raise BookUnavailable(self.last_unavailable, reason, code) from exc
        except PublicRequestDeadline as exc:
            self.last_unavailable = str(exc)
            raise BookUnavailable(self.last_unavailable, "request_budget_deadline") from None
        except (FeedError, httpx.HTTPError, TimeoutError) as exc:
            self.last_unavailable = "Could not read an open Polymarket US order book."
            reason = "market_not_open" if str(exc).startswith("book MARKET_STATE_") else "request_failed"
            if reason == "market_not_open":
                now = self.clock()
                self._direct_closures = {key: at for key, at in self._direct_closures.items()
                                         if now - at <= BOOK_MAX_AGE}
                self._direct_closures[slug] = now
            raise BookUnavailable(self.last_unavailable, reason) from None
        self.cached = (slug, book)
        self.last_unavailable = None
        return book

    async def book(self, slug, requested_after=None):
        book = self.exit_snapshot(slug, requested_after)
        if book is not None:
            return book
        if self.stream_only:
            raise BookUnavailable(_STREAM_UNAVAILABLE, "stream_unavailable")
        task = self._start_direct(slug)
        if task is None:
            raise FeedError(self.last_unavailable or "No fresh WTI order book yet. Waiting for the next capture.")
        try:
            book = await task
        except BookUnavailable:
            if self.stream_snapshot is not None and (book := self.exit_snapshot(slug, requested_after)) is not None:
                return book
            raise
        if self.stream_snapshot is not None:
            verified = self.exit_snapshot(slug, requested_after)
            if verified is not None:
                return verified
        if not usable(book, self.clock(), requested_after) or self.stream_snapshot is not None:
            self.last_unavailable = ("The WTI order book had no exchange timestamp." if book.exchange_at is None
                                     else "The WTI order book was outside the live freshness limits.")
            raise FeedError(self.last_unavailable)
        self.last_unavailable = None
        return book

    async def entry_book(self, slug, ready_at, deadline, observe, *, can_select=None, assess=None):
        """Wait for an eligible quote, allowing one budgeted direct REST fallback."""
        seen = set()
        last_failure = None
        last_failure_order = (float("-inf"), False)
        assessment_failure = None

        def report(event):
            nonlocal last_failure, last_failure_order, assessment_failure
            reason = event["reason"]
            message = None
            if event.get("assessment_rejected"):
                message = event.get("message") or _ASSESSMENT_MESSAGES.get(reason) or "The WTI quote did not qualify."
                assessment_failure = BookUnavailable(message, reason)
            elif reason == "exchange_timestamp_missing":
                message = "The WTI order book had no exchange timestamp."
            elif reason in _FRESHNESS_REASONS:
                message = "The WTI order book was outside the live freshness limits."
            elif reason == "market_not_open":
                message = "The WTI contract order book was not open."
            if message is not None:
                event_at = next(event[key] for key in ("received_at", "state_at", "tape_at", "observed_at")
                                if event.get(key) is not None)
                order = (event_at, event["source"] in {"direct_rest", "stream"})
                if order > last_failure_order:
                    last_failure = BookUnavailable(message, reason)
                    last_failure_order = order
            key = (event["source"], reason, event["received_at"], event.get("tape_at"), event.get("state_at"),
                   event.get("http_status"), event.get("assessed"))
            if key not in seen and len(seen) < MAX_QUOTE_OBSERVATIONS:
                seen.add(key)
                observe(event)

        def inspect_direct(finished):
            if finished.cancelled():
                return None
            now = self.clock()
            try:
                result = finished.result()
            except BookUnavailable as exc:
                fields = {}
                if exc.reason == "market_not_open":
                    fields["state_at"] = self._direct_closures.get(slug)
                if exc.status is not None:
                    fields["http_status"] = exc.status
                report(_observation("direct_rest", now, ready_at, exc.reason, **fields))
                return exc
            reason = _freshness_reason(result, now, ready_at, exchange_max_age=ENTRY_EXCHANGE_MAX_AGE)
            if reason != "usable":
                report(_observation("direct_rest", now, ready_at, reason, result))
            return None

        task = None
        signal = (slug, ready_at)
        started_at = self.clock()
        direct_failure = None
        book = None
        if not self.stream_only:
            self._entry_direct_attempts = {key: expires for key, expires in self._entry_direct_attempts.items()
                                           if expires > started_at}
            if signal in self._entry_direct_attempts:
                report(_observation("direct_rest", started_at, ready_at, "direct_attempt_already_used"))
        try:
            async with asyncio.timeout(max(0, deadline - self.clock())):
                while self.clock() < deadline:
                    row = None if self.stream_only else await asyncio.to_thread(self._recorded, slug, self.clock())
                    if task is not None and task.done():
                        direct_failure, task = inspect_direct(task), None
                    account_ready = can_select is None or can_select()
                    book, allow_rest = self._select_entry(slug, row, ready_at, report,
                                                          assess if account_ready else None)
                    now = self.clock()
                    if now >= deadline:
                        break
                    if book is not None and account_ready:
                        return book
                    if not allow_rest and task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        task = None
                    if book is None and allow_rest and signal not in self._entry_direct_attempts and now >= ready_at:
                        task = self._start_direct(slug)
                        if task is not None:
                            self._entry_direct_attempts[signal] = max(deadline, ready_at + SIGNAL_TTL)
                            direct_failure = None
                            report(_observation("direct_rest", now, ready_at, "direct_attempt_started"))
                        elif self.last_unavailable and "HTTP 429" in self.last_unavailable:
                            direct_failure = BookUnavailable(self.last_unavailable, "direct_cooldown")
                            report(_observation("direct_rest", now, ready_at, "direct_cooldown"))
                    delay = min(TAPE_POLL_INTERVAL, deadline - now)
                    if task is None:
                        await asyncio.sleep(delay)
                    else:
                        await asyncio.wait((task,), timeout=delay)
        except TimeoutError:
            pass
        finally:
            direct_pending = task is not None and not task.done()
            if direct_pending:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            elif task is not None:
                direct_failure = inspect_direct(task)
        if book is not None and can_select is not None and not can_select():
            raise BookUnavailable("The signal expired before the account checks and a fresh WTI book were both ready.",
                                  "account_checks_deadline")
        if direct_pending:
            report(_observation("direct_rest", self.clock(), ready_at, "direct_deadline"))
        if last_failure is not None and last_failure.reason == "market_not_open":
            raise last_failure
        if self.stream_only:
            raise assessment_failure or last_failure or BookUnavailable(_STREAM_UNAVAILABLE, "stream_unavailable")
        raise assessment_failure or direct_failure or last_failure or BookUnavailable(
            "The signal expired while waiting for a fresh WTI book.", "direct_deadline" if direct_pending else "book_deadline")
