"""One real-money Oil D pilot, consuming the same entry intents as paper replay."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
from uuid import uuid4

import httpx

from ..trading.request_budget import RequestBudget
from ..trading.venue import BASE, PublicRequestDeadline, ReadOnlyVenueClient, public_request_scope
from ..updown.feed import Feed, FeedError
from ..updown.model import taker_fee
from .commodity import WTI
from .live_books import ENTRY_EXCHANGE_MAX_AGE, EXIT_BUFFER, SIGNAL_TTL, SUBMISSION_BUFFER, LiveBooks, usable
from .live_store import PILOT_LOSS_LIMIT, PILOT_LOSS_PAUSE, LiveStore, live_path
from .live_venue import AmbiguousOrder, LiveVenue, PublicMarketUnavailable, TERMINAL_STATES, VenueError
from .oil_d_shadow_strategy import simulate_arrival
from .oil_d_stream_signals import OilDStreamSignals
from .tape import tape_path

LOG = logging.getLogger(__name__)
ZERO = Decimal(0)
MAX_ENTRY = Decimal("1.00")
MAX_LOSS = PILOT_LOSS_LIMIT
FEE_RESERVE = Decimal("0.02")
DEPTH_SHARE = Decimal("0.25")
FINAL_STATES = TERMINAL_STATES
RULES_MAX_AGE = 90.0
REFRESH_INTERVAL = 45.0
RETRY_INTERVAL = 8.0
PREFETCH_TIMEOUT = 3.0
STREAM_MODE = "stream_end_to_end"
STREAM_SCAN_INTERVAL = 5.0


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite execution value")
    return result


def configured_budget():
    amount = decimal(os.getenv("WTI_LIVE_BUDGET_USD", "12"))
    if not 0 < amount <= 12:
        raise ValueError("The Oil D pilot budget must be between $0 and $12.")
    return amount


def floor_to(value, increment):
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def signals(path: Path, since, now, action, slug=None):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        rows = db.execute("""
            SELECT t.slug, m.start, m.end, t.at, t.payload
            FROM tape t JOIN markets m ON m.slug=t.slug
            WHERE t.source='intent' AND t.kind='order' AND t.at>=? AND t.at<=?
            AND json_extract(t.payload,'$.model')='D' AND json_extract(t.payload,'$.action')=?
            AND m.start BETWEEN ? AND ? AND m.end>? AND m.end-m.start=900
            AND (? IS NULL OR t.slug=?) ORDER BY t.at,t.id
        """, (since, now, action, now - 900, now, now, slug, slug)).fetchall()
    return [(slug, start, end, at, json.loads(payload)) for slug, start, end, at, payload in rows]


def paper_trade(directory, trade, signal_at):
    path = directory / "models" / "D" / "paper.sqlite3"
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT payload FROM trades WHERE asset='wti-15m' AND window_start=?",
                         (trade["window_start"],)).fetchone()
    if row is None:
        return None
    paper = json.loads(row[0])
    if paper.get("market_slug") == trade["slug"] and paper.get("signal_at") == signal_at:
        return {k: paper.get(k) for k in ("status", "entry_price", "quantity", "net_pnl", "cost", "exit_reason")}
    return None


class EntryMarkets:
    """Cache only verified rules for the currently tradable WTI window."""

    def __init__(self, venue, clock):
        self.venue, self.clock = venue, clock
        self._checked = None
        self._last_attempt = None
        self._lock = asyncio.Lock()

    def verified(self, slug, end):
        if self._checked is not None:
            rules, checked_at = self._checked
            now = self.clock()
            if (rules.slug == slug and rules.closes_at == end and end - 900 <= now < end
                    and 0 <= now - checked_at <= RULES_MAX_AGE):
                return rules
        return None

    def status(self):
        if self._checked is not None:
            rules, checked_at = self._checked
            if self.verified(rules.slug, rules.closes_at) is not None:
                return {"slug": rules.slug, "verified_at": checked_at}
        return None

    async def _load(self, slug, end, priority, deadline):
        self._last_attempt = (slug, self.clock())
        try:
            with public_request_scope(priority, deadline):
                rules = await self.venue.market(slug)
        except PublicMarketUnavailable:
            # An outage does not invalidate an already verified, time-bounded result.
            raise
        except VenueError:
            self._invalidate(slug)
            raise
        if rules.slug != slug or rules.closes_at != end or not rules.is_open:
            self._invalidate(slug)
            raise VenueError("The venue did not verify this 15-minute contract as open.")
        self._checked = (rules, self.clock())
        return rules

    def _invalidate(self, slug):
        if self._checked is not None and self._checked[0].slug == slug:
            self._checked = None

    async def prime(self, window):
        now = self.clock()
        if not window.start <= now < window.end or self._lock.locked():
            return
        if (self._last_attempt is not None and self._last_attempt[0] == window.slug
                and now - self._last_attempt[1] < RETRY_INTERVAL):
            return
        warm = self.verified(window.slug, window.end)
        if warm is not None and now - self._checked[1] < REFRESH_INTERVAL:
            return
        deadline = min(now + PREFETCH_TIMEOUT, window.end)
        async with asyncio.timeout(deadline - now), self._lock:
            await self._load(window.slug, window.end, "wti_signal", deadline)

    async def for_entry(self, slug, end, deadline):
        rules = self.verified(slug, end)
        if rules is not None:
            return rules
        # On startup or rollover, wait for the same in-flight read before fetching again.
        async with self._lock:
            return self.verified(slug, end) or await self._load(slug, end, "live", deadline)


@dataclass(frozen=True)
class EntrySizing:
    side: str
    price: Decimal
    minimum: Decimal
    max_quantity: Decimal

    def assess(self, book):
        asks = book.side_asks(self.side)
        best_ask = decimal(asks[0][0]) if asks else None
        depth = sum((decimal(size) for price, size in asks if decimal(price) <= self.price), ZERO)
        quantity = floor_to(min(self.max_quantity, DEPTH_SHARE * depth), self.minimum)
        details = {"best_ask": str(best_ask) if best_ask is not None else None, "eligible_depth": str(depth),
                   "required_depth": str(self.minimum / DEPTH_SHARE), "eligible_quantity": str(quantity)}
        if best_ask is not None and best_ask > self.price:
            return "price_above_limit", details
        if quantity < self.minimum:
            return "insufficient_depth", details
        return None, details


class Pilot:
    def __init__(self, store, venue, feed, directory, amount, clock=time.time, entries_allowed=True, *,
                 stream_snapshot=None, book_mode=None):
        self.store, self.venue, self.feed = store, venue, feed
        self.directory, self.clock = directory, clock
        self.amount, self.entries_allowed = amount, entries_allowed
        self.pilot_run = None
        self.tape = tape_path(directory)
        self.book_mode = book_mode or ("stream_first" if stream_snapshot is not None else "rest")
        native_stream = self.book_mode == STREAM_MODE
        if native_stream and stream_snapshot is None:
            raise ValueError("WebSocket live entries require a stream source.")
        self.books = LiveBooks(self.tape, feed, clock, stream_snapshot=stream_snapshot, stream_only=native_stream)
        # Open positions keep the existing fresh REST fallback if their stream is interrupted.
        self.exit_books = LiveBooks(self.tape, feed, clock, stream_snapshot=stream_snapshot) if native_stream else self.books
        self.settings = WTI.settings()
        self.stream_signals = (OilDStreamSignals(directory, stream_snapshot, clock, settings=self.settings)
                               if native_stream else None)
        self.stream_pending = None
        self.stream_scan_at = float("-inf")
        self.stream_check = None
        self.entry_markets = EntryMarkets(venue, clock)
        self.product = next(p for p in self.settings.products if p.key == "wti-15m")
        self.started = clock()
        self.balance_at = float("-inf")
        self.comparison_at = float("-inf")
        self.comparison = {}
        self.buying_power = None
        self.report = {}

    @property
    def pilot_budget(self):
        return min(self.amount, decimal(self.pilot_run["starting_budget"])) if self.pilot_run is not None else self.amount

    def status(self, state, message, **fields):
        self.report = {"state": state, "message": message, "model": "D", "market": "wti-15m",
                       "book_mode": self.book_mode,
                       "signal_source": "stream" if self.stream_signals is not None else "paper",
                       "pilot_budget": str(self.pilot_budget), "entry_cap": str(MAX_ENTRY), "loss_cap": str(MAX_LOSS),
                       "buying_power": str(self.buying_power) if self.buying_power is not None else None, **fields}
        if self.pilot_run is not None:
            self.report["pilot_run_key"] = self.pilot_run["pilot_key"]

    async def tick(self):
        now = self.clock()
        self.pilot_run = self.store.active_pilot()
        active = self.store.active_trade()
        trades = self.store.trades(limit=None)
        closed = [t for t in trades if t["payload"].get("net_pnl") is not None]
        total = sum((decimal(t["payload"]["net_pnl"]) for t in closed), ZERO)
        risk_total = total
        if self.pilot_run is not None:
            risk_total -= decimal(self.pilot_run["baseline_realized_pnl"])
        entries_enabled = self.entries_allowed and self.store.enabled()
        if active:
            self.stream_pending = None
            # Allow signed reconciliation/cancellation during a public cooldown.
            with public_request_scope("exposure", self.clock() + 2):
                await self.manage(active)
            if self.stream_signals is None and entries_enabled and risk_total > -MAX_LOSS:
                for candidate in self.buy_signals():
                    self.note_unavailable_entry(candidate, "The live pilot already had an unresolved trade.")
        elif not entries_enabled:
            self.stream_pending = None
            self.status("paused", "New entries are off. The account has no active pilot position.")
        elif risk_total <= -MAX_LOSS:
            self.stream_pending = None
            self.store.disable(now, PILOT_LOSS_PAUSE)
            self.status("paused", "The pilot loss limit stopped new entries.")
        else:
            self.status("waiting", "Waiting for a fresh Oil D 15-minute entry signal.")
            used_windows = {trade["window_start"] for trade in trades}
            if self.stream_signals is not None:
                await self.scan_stream_entry(used_windows, risk_total)
                self.report["stream_signal_check"] = self.stream_check
            else:
                for candidate in self.buy_signals():
                    if candidate[1] in used_windows:
                        self.note_unavailable_entry(candidate, "The pilot already used its entry attempt for this window.")
                    elif await self.enter(candidate, risk_total):
                        break
            if self.stream_pending is None and self.clock() - self.balance_at > 30:
                await self.refresh_balance()
        self.store.publish_expired_signals(self.clock(), SIGNAL_TTL, EXIT_BUFFER)
        self.report["buying_power"] = str(self.buying_power) if self.buying_power is not None else None
        self.report["net_pnl"] = str(total)
        if self.pilot_run is not None:
            self.report.update(pilot_net_pnl=str(risk_total),
                               pilot_equity=str(decimal(self.pilot_run["starting_budget"]) + risk_total))
        self.report["trades"] = len(closed)
        self.report["pnl_estimated"] = any(not t["payload"].get("fees_exact") for t in closed)
        if now - self.comparison_at >= 20:
            self.comparison = self.compare_paper(closed)
            self.comparison_at = now
        self.report.update(self.comparison)

    async def scan_stream_entry(self, used_windows, net_pnl):
        now = self.clock()
        if self.stream_pending is not None:
            window, intent = self.stream_pending
            if now >= intent.deadline:
                self.stream_pending = None
                self.stream_check = "The WebSocket signal expired before account checks"
                return
            if window.start in used_windows:
                self.stream_pending = None
            elif now >= intent.ready_at:
                self.stream_pending = None
                # The ordinary entry pipeline applies the submission buffer to this raw model cutoff.
                payload = {"action": "buy", "signal_source": "stream", "side": intent.side,
                           "limit": str(intent.limit), "quantity": str(intent.quantity), "budget": str(intent.budget),
                           "theta": str(intent.theta), "fair_up": intent.fair_up, "min_edge": str(intent.min_edge),
                           "ready_at": intent.ready_at, "deadline": window.end - self.settings.entry_before_end_seconds,
                           "signal_price_to_beat": window.price_to_beat,
                           "signal_book_received_at": intent.signal_book_at,
                           "signal_book_exchange_at": intent.signal_book_exchange_at}
                candidate = (window.slug, window.start, window.end, intent.signal_at, payload)
                await self.enter(candidate, net_pnl, stream_intent=intent)
                return
            else:
                return
        if now < self.stream_scan_at:
            return
        self.stream_scan_at = now + STREAM_SCAN_INTERVAL
        window, inputs = await asyncio.to_thread(self.stream_signals.reference, now)
        if window is not None and window.start in used_windows:
            self.stream_check = "This contract already used its live entry attempt."
            return
        result = self.stream_signals.evaluate(window, inputs)
        self.stream_check = result.reason
        if result.intent is not None:
            self.stream_pending = (window, result.intent)

    def buy_signals(self):
        now = self.clock()
        return signals(self.tape, max(self.started, now - SIGNAL_TTL - 2), now, "buy")

    def compare_paper(self, closed):
        matched = 0
        paired_paper = paired_live = ZERO
        for trade in closed:
            paper = paper_trade(self.directory, trade, trade["payload"]["signal_at"])
            if paper and paper["status"] == "CLOSED":
                matched += 1
                paired_paper += decimal(paper["net_pnl"])
                paired_live += decimal(trade["payload"]["net_pnl"])
        return {"matched_trades": matched, "paper_net_pnl": str(paired_paper) if matched else None,
                "paired_live_net_pnl": str(paired_live) if matched else None}

    async def refresh_balance(self):
        self.buying_power = await self.venue.buying_power()
        self.balance_at = self.clock()

    def entry_is_current(self, payload, end):
        now = self.clock()
        ready, deadline = float(payload["ready_at"]), min(float(payload["deadline"]), end - EXIT_BUFFER)
        return self.started <= ready <= now <= ready + SIGNAL_TTL and now + SUBMISSION_BUFFER < deadline

    def note_unavailable_entry(self, candidate, reason):
        slug, start, end, at, payload = candidate
        if self.entry_is_current(payload, end):
            self.store.record_signal(slug, start, end, at, {**payload, "live_entry_check": reason})

    async def entry_checks(self, slug, end, deadline):
        checks = [asyncio.create_task(check) for check in (
            self.entry_markets.for_entry(slug, end, deadline), self.venue.positions(),
            self.venue.open_orders(), self.venue.buying_power(),
        )]
        try:
            return await asyncio.gather(*checks)
        finally:
            for check in checks:
                check.cancel()
            await asyncio.gather(*checks, return_exceptions=True)

    async def enter(self, candidate, net_pnl, *, stream_intent=None):
        slug, start, end, at, payload = candidate
        if ((self.stream_signals is not None) != (stream_intent is not None)
                or not self.entry_is_current(payload, end) or payload.get("side") not in ("UP", "DOWN")):
            return False
        if end - start != 900 or not self.store.enabled():
            return False
        signal_id = self.store.record_signal(slug, start, end, at, {**payload,
            "live_entry_check": "The signal expired before the live entry checks finished."})
        ready_at = float(payload["ready_at"])
        cutoff = min(float(payload["deadline"]), end - EXIT_BUFFER)
        deadline = min(ready_at + SIGNAL_TTL, cutoff - SUBMISSION_BUFFER)
        started = self.clock()
        timing = {
            "slug": slug, "signal_at": at, "ready_at": ready_at, "window_end": end, "deadline": deadline,
            "side": payload["side"], "limit": payload["limit"], "depth_share": str(DEPTH_SHARE),
            "attempt_started_at": started, "max_exchange_age_seconds": ENTRY_EXCHANGE_MAX_AGE,
            "book_mode": self.book_mode, "signal_source": payload.get("signal_source", "paper"),
            "quotes": [], "outcome": "interrupted",
        }
        if stream_intent is not None:
            timing.update(fair_up=stream_intent.fair_up, min_edge=str(stream_intent.min_edge),
                          signal_book_received_at=stream_intent.signal_book_at,
                          signal_book_exchange_at=stream_intent.signal_book_exchange_at)

        def observe(quote, stage="search"):
            quotes = timing["quotes"]
            result = {"usable": "usable", "direct_attempt_started": "started"}.get(quote.get("reason"), "rejected")
            checked_at = quote["observed_at"]
            event = {"stage": stage, "result": result,
                     "checked_at": checked_at, "entry_seconds_left": max(0, deadline - checked_at), **quote}
            if len(quotes) >= 32:
                # Keep initial observations and the most recent evidence before sending.
                del quotes[8]
                timing["quote_observations_truncated"] = True
            quotes.append(event)
            if stage == "pre_submit" and event["result"] == "usable":
                timing["final_quote_source"] = event["source"]

        sizing_ready = asyncio.get_running_loop().create_future()

        async def find_book():
            timing["book_search_started_at"] = self.clock()
            try:
                return await self.books.entry_book(slug, ready_at, deadline, observe, can_select=sizing_ready.done,
                                                   assess=lambda book: sizing_ready.result()(book))
            finally:
                timing["book_search_finished_at"] = self.clock()

        with public_request_scope("live", deadline):
            quote_task = asyncio.create_task(find_book())
        try:
            return await self.finish_entry(candidate, net_pnl, signal_id, timing, quote_task, sizing_ready, observe,
                                           stream_intent=stream_intent)
        finally:
            quote_task.cancel()
            await asyncio.gather(quote_task, return_exceptions=True)
            finished = self.clock()
            timing["attempt_finished_at"] = finished
            try:
                await asyncio.to_thread(self.store.record_entry_timing, finished, timing)
            except (sqlite3.Error, ValueError):
                LOG.warning("Could not save Oil D entry timing", exc_info=True)

    async def finish_entry(self, candidate, net_pnl, signal_id, timing, quote_task, sizing_ready, observe, *,
                           stream_intent=None):
        slug, _, end, at, payload = candidate
        ready_at, deadline = timing["ready_at"], timing["deadline"]

        def skip(code, message, state="waiting"):
            timing.update(outcome="skipped", reason=code, message=message)
            self.store.check_signal(signal_id, message)
            self.status(state, message)
            return True

        try:
            async with asyncio.timeout(max(0, deadline - self.clock())):
                market, positions, working, buying_power = await self.entry_checks(slug, end, deadline)
        except TimeoutError:
            return skip("account_timeout", "The signal expired while checking account and contract status.")
        except VenueError as exc:
            return skip("account_or_contract_unavailable", str(exc), state="blocked")
        timing["checks_completed_at"] = self.clock()
        if any(positions.values()) or working:
            return skip("account_has_exposure", "The exchange account has another position or open order.", state="blocked")
        self.buying_power, self.balance_at = buying_power, self.clock()
        if not self.entry_is_current(payload, end):
            return skip("signal_expired_after_checks", "The signal expired while checking account and market status.")

        tick, step = market.tick_size, market.min_quantity
        price = floor_to(decimal(payload["limit"]), tick)
        timing.update(limit=str(price), minimum_quantity=str(step))
        if not tick <= price <= 1 - tick:
            return skip("invalid_limit", "The signal’s limit price was outside the exchange’s price range.")

        room = MAX_LOSS + min(net_pnl, ZERO)
        budget = min(MAX_ENTRY, self.pilot_budget, buying_power, room, decimal(payload["budget"]))
        fee_per_contract = max(Decimal("0.03"), decimal(payload["theta"]) / 4)
        max_quantity = min(decimal(payload["quantity"]), (budget - FEE_RESERVE) / (price + fee_per_contract))
        if floor_to(max_quantity, step) < step:
            return skip("budget_below_minimum", "The risk budget or signal size allowed less than the exchange’s minimum size.")
        timing["max_order_quantity"] = str(floor_to(max_quantity, step))
        assess = EntrySizing(payload["side"], price, step, max_quantity).assess
        if stream_intent is not None:
            def assess(book):
                estimate = simulate_arrival(stream_intent, book, now=self.clock(), minimum_quantity=step,
                                            price_tick=tick, buying_power=buying_power, pilot_budget=self.pilot_budget,
                                            net_pnl=net_pnl)
                evidence = {key: str(value) for key in ("best_ask", "required_depth", "eligible_depth", "eligible_quantity")
                            if (value := getattr(estimate, key)) is not None}
                if estimate.best_ask is not None:
                    ask = float(estimate.best_ask)
                    fair = stream_intent.fair_up if stream_intent.side == "UP" else 1 - stream_intent.fair_up
                    evidence["edge_after_fees"] = fair - ask - taker_fee(ask, float(stream_intent.theta))
                return (None if estimate.fill is not None else estimate.reason), evidence

        sizing_ready.set_result(assess)
        try:
            book = await quote_task
        except TimeoutError:
            timing["decision_at"] = self.clock()
            return skip("book_timeout", "The signal expired while waiting for a fresh WTI book.")
        except FeedError as exc:
            timing["decision_at"] = self.clock()
            reason = getattr(exc, "reason", "book_unavailable")
            last_depth = next((q for q in reversed(timing["quotes"]) if q.get("reason") == reason
                               and "eligible_depth" in q), {})
            message = str(exc)
            if reason == "price_above_limit" and last_depth.get("best_ask") is not None:
                ask = decimal(last_depth["best_ask"]) * 100
                message = f"Best offer {ask:g}¢ stayed above the {(price * 100):g}¢ limit."
            elif reason == "insufficient_depth" and last_depth:
                message = (f"Too little displayed liquidity at the {(price * 100):g}¢ limit. "
                           f"Saw {last_depth['eligible_depth']} contracts at the limit or better. "
                           f"Needed {last_depth['required_depth']}.")
            return skip(reason, message)

        checked = self.clock()
        timing.update(quote_selected_at=checked, decision_at=checked, selected_book_received_at=book.at)
        rejection, details = assess(book)
        if rejection is not None:
            return skip(rejection, "The entry quote changed before the order could be prepared.")
        timing.update(details)
        quantity = decimal(details["eligible_quantity"])
        if not self.entry_is_current(payload, end):
            return skip("signal_expired_before_submit", "The signal expired before the order could be submitted.")
        if self.entry_markets.verified(slug, end) != market:
            return skip("contract_changed", "The verified contract rules changed or expired before submission.")

        if stream_intent is not None:
            window, inputs = await asyncio.to_thread(self.stream_signals.reference, self.clock())
            now = self.clock()
            if (window is None or window.slug != slug or window.end != end
                    or window.price_to_beat != payload["signal_price_to_beat"]
                    or not self.stream_signals.is_entry_window(window, now)):
                return skip("contract_changed", "The WebSocket signal's contract is no longer open for entries.")
            if (window.minimum_quantity is None or decimal(window.minimum_quantity) != step
                    or window.price_tick is None or decimal(window.price_tick) != tick
                    or decimal(window.theta) != stream_intent.theta):
                return skip("contract_changed", "The public contract terms differ from the verified entry terms.")
            if not self.stream_signals.inputs_are_current(inputs, now):
                return skip("index_unavailable", "WTI reference prices expired before submission.")
            hold = self.stream_signals.entry_hold(window, inputs, now)
            if hold:
                return skip("news_or_shock_hold", hold)
            if not self.entry_is_current(payload, end) or self.entry_markets.verified(slug, end) != market:
                return skip("signal_expired_before_submit", "The signal or market check expired before submission.")

        request = self.request(slug, payload["side"], "buy", quantity, price,
                               signal_at=at, fee_per_contract=str(fee_per_contract))
        if stream_intent is not None:
            request.update(signal_source="stream", quote_source="stream")
        order = self.store.claim_entry(signal_id, uuid4().hex, self.clock(), request)
        if not order or not self.store.authorize_entry(order["id"], self.clock()):
            return skip("entry_not_authorized", "The live entry was no longer authorized.")

        final_book = self.books.entry_snapshot(slug, ready_at, lambda event: observe(event, "pre_submit"),
                                               assess=assess)
        checked = self.clock()
        timing["decision_at"] = checked
        final_quantity = ZERO
        if final_book is not None:
            rejection, final_details = assess(final_book)
            if rejection is None:
                final_quantity = decimal(final_details["eligible_quantity"])
            timing["final_eligible_depth"] = final_details.get("eligible_depth")
            timing["final_book_received_at"] = final_book.at
        if not (final_book is not None and usable(final_book, checked, ready_at, exchange_max_age=ENTRY_EXCHANGE_MAX_AGE)
                and final_quantity >= quantity and self.entry_is_current(payload, end)
                and self.entry_markets.verified(slug, end) == market):
            message = "The quote, depth, signal, or contract check changed before submission."
            self.store.update_order(order["id"], "ORDER_STATE_CANCELED", self.clock(), payload={"reason": message})
            return skip("quote_or_rules_changed_at_send", message)

        timing.update(submit_started_at=self.clock(), quantity=str(quantity), outcome="submission_started")
        try:
            saved = await self.submit(order)
        except AmbiguousOrder:
            timing.update(outcome="submission_unconfirmed", reason="ambiguous_order")
            raise
        except VenueError:
            timing.update(outcome="submission_unconfirmed", reason="venue_error")
            raise
        timing.update(outcome="submitted", acknowledged=bool(saved["venue_id"]), order_state=saved["state"],
                      acknowledged_at=saved["updated_at"], filled=saved["payload"].get("filled"))
        self.status("working", "An Oil D entry was submitted. Checking exchange fills.")
        return True

    def request(self, slug, side, action, quantity, price, expires_at=None, **extra):
        return {"slug": slug, "side": side, "action": action, "quantity": str(quantity), "price": str(price),
                "expires_at": expires_at, "prepared_at": self.clock(), **extra}

    async def submit(self, order):
        request = order["request"]
        if order["kind"] != "entry":
            order = self.store.update_order(order["id"], "sending", self.clock())
        try:
            result = await self.venue.submit_limit(slug=request["slug"], side=request["side"],
                                                  action=request["action"], quantity=decimal(request["quantity"]),
                                                  price=decimal(request["price"]), key=order["key"],
                                                  expires_at=request["expires_at"])
        except AmbiguousOrder as exc:
            self.store.update_order(order["id"], "UNKNOWN", self.clock(), venue_id=exc.order_id)
            self.store.disable(self.clock(), "Exchange order submission could not be confirmed")
            raise
        except VenueError:
            self.store.update_order(order["id"], "ORDER_STATE_REJECTED", self.clock())
            raise
        try:
            return self.save_order(order, result)
        except VenueError:
            self.store.update_order(order["id"], "UNKNOWN", self.clock(), venue_id=result.id)
            self.store.disable(self.clock(), "The exchange response disagreed with the submitted order")
            raise

    def save_order(self, order, result):
        request = order["request"]
        if (result.slug != request["slug"] or result.side != request["side"] or result.action != request["action"]
                or result.id != (order["venue_id"] or result.id) or result.price != decimal(request["price"])
                or result.quantity != decimal(request["quantity"]) or
                not decimal(order["payload"].get("filled", "0")) <= result.filled <= result.quantity):
            raise VenueError("Exchange order details disagree with the live journal")
        actual = {"filled": str(result.filled), "average_price": str(result.average_price)
                  if result.average_price is not None else None,
                  "fees": str(result.fees) if result.fees is not None else None,
                  "remaining": str(result.remaining)}
        now = self.clock()
        saved = self.store.update_order(order["id"], result.state, now, venue_id=result.id, payload=actual)
        for receipt in result.executions:
            if receipt.fee is not None:
                self.store.record_fill(order["id"], receipt.id, receipt.at if receipt.at is not None else now,
                                       receipt.quantity, receipt.price, receipt.fee,
                                       {"exchange_at": receipt.at, "observed_at": now})
        return saved

    async def refresh_order(self, order):
        if order["venue_id"] and (order["state"] not in FINAL_STATES or
                                  (self.filled(order) and not order["payload"].get("average_price"))):
            return self.save_order(order, await self.venue.order(order["venue_id"]))
        if (order["venue_id"] and self.filled(order) and order["payload"].get("fees") is None
                and not order["payload"].get("fee_refresh_attempted")):
            refreshed = self.store.update_order(order["id"], order["state"], self.clock(),
                                                payload={"fee_refresh_attempted": True})
            try:
                result = await self.venue.order(order["venue_id"])
            except VenueError:
                return refreshed
            return self.save_order(refreshed, result)
        return order

    @staticmethod
    def filled(order):
        return decimal(order["payload"].get("filled", "0"))

    @staticmethod
    def gross(order):
        quantity = Pilot.filled(order)
        return quantity * decimal(order["payload"]["average_price"]) if quantity else ZERO

    async def manage(self, trade):
        slug, end = trade["slug"], trade["window_end"]
        rows = []
        for order in self.store.orders(trade["id"]):
            if order["state"] == "prepared":
                # An order cannot leave this process until its sending state is durable.
                rows.append(self.store.update_order(order["id"], "ORDER_STATE_CANCELED", self.clock(),
                                                    payload={"reason": "Worker restarted before submission"}))
            else:
                rows.append(await self.refresh_order(order))
        if any(not o["venue_id"] and o["state"] not in FINAL_STATES for o in rows):
            self.store.disable(self.clock(), "An order needs exchange reconciliation")
            self.status("blocked", "Order state is unknown. New entries are off. Inspect orders and fills at Polymarket.")
            return
        entry = next(order for order in rows if order["kind"] == "entry")
        exits = [order for order in rows if order["kind"] != "entry"]
        if entry["state"] not in FINAL_STATES:
            self.status("working", "Waiting for the IOC entry to reach a final exchange state.")
            return
        quantity = self.filled(entry)
        if not quantity:
            state = entry["state"].removeprefix("ORDER_STATE_").lower().replace("_", " ")
            reason = entry["payload"].get("reason")
            if not reason and entry["state"] == "ORDER_STATE_CANCELED":
                reason = "The exchange canceled the immediate-or-cancel order with zero fills at the limit."
            reason = reason or f"The exchange marked the entry {state} with zero fills."
            self.store.close_trade(trade["id"], self.clock(), "NO_FILL", {
                "title": "Entry unfilled" if entry["venue_id"] else "Entry skipped",
                "message": reason, "side": entry["request"]["side"],
                "price": entry["request"]["price"], "state": entry["state"],
            })
            self.status("waiting", reason)
            return
        if any(self.filled(o) and not o["payload"].get("average_price") for o in rows):
            raise VenueError("Waiting for the exchange's confirmed average fill price")
        remaining = quantity - sum((self.filled(o) for o in exits), ZERO)
        if remaining < 0:
            raise VenueError("Exit fills exceed the confirmed entry quantity")
        pending = next((o for o in exits if o["state"] not in FINAL_STATES), None)
        # The journal owns at most one exit order at a time, including cancellation races.
        if pending and any(o is not pending and o["state"] not in FINAL_STATES for o in exits):
            raise VenueError("More than one exit is working for the same position")
        if self.clock() >= end:
            if pending:
                await self.cancel(pending)
                return
            if remaining:
                settlement = await self.feed.settlement(self.product, trade["window_start"])
                if settlement is None or settlement.slug != slug:
                    self.status("settling", "Market closed. Waiting for the official payout.")
                    return
                payout = decimal(settlement.yes_payout)
                if entry["request"]["side"] == "DOWN":
                    payout = 1 - payout
                self.close(trade, entry, exits, payout * remaining, "settlement")
                return
        if not remaining:
            if pending:
                await self.cancel(pending)
            else:
                self.close(trade, entry, exits, ZERO, exits[-1]["kind"])
            return
        await self.manage_exits(trade, entry, exits, pending, remaining)

    async def cancel(self, order):
        now = self.clock()
        if now - float(order["payload"].get("cancel_requested_at", 0)) < 2:
            return
        self.store.update_order(order["id"], order["state"], now, venue_id=order["venue_id"],
                                payload={"cancel_requested_at": now})
        await self.venue.cancel(order["venue_id"], order["request"]["slug"])

    async def manage_exits(self, trade, entry, exits, pending, remaining):
        slug, end, side = trade["slug"], trade["window_end"], entry["request"]["side"]
        cutoff = end - EXIT_BUFFER
        if self.clock() >= cutoff:
            if pending:
                await self.cancel(pending)
            return
        market = await self.venue.market(slug)
        tick, step = market.tick_size, market.min_quantity
        entry_price = decimal(entry["payload"]["average_price"])
        profit_fraction = decimal(self.settings.take_profit_target or .5)
        stop_fraction = decimal(self.settings.stop_loss or .5)
        target = min(1 - tick, ((entry_price + profit_fraction * (1 - entry_price)) / tick)
                     .to_integral_value(rounding=ROUND_UP) * tick)
        stop = max(tick, floor_to(entry_price * (1 - stop_fraction), tick))
        self.status("position", "Managing the confirmed live position.",
                    exposure=str(self.gross(entry)), position={"slug": slug, "side": side,
                    "quantity": str(remaining), "average_price": str(entry_price), "take_profit": str(target),
                    "stop": str(stop)})
        if self.clock() >= cutoff or not market.is_open:
            if pending:
                await self.cancel(pending)
            return
        book = await self.exit_books.book(slug)
        bids = book.side_bids(side)
        best_bid = decimal(bids[0][0]) if bids else None
        trigger = trade["payload"].get("exit_reason")
        discretionary_limit = trade["payload"].get("exit_limit")
        if best_bid is not None and best_bid <= stop and trigger != "stop_loss":
            trigger = "stop_loss"
            self.store.set_trade(trade["id"], self.clock(), {"exit_reason": trigger})
        now = self.clock()
        if trigger is None and entry["request"].get("signal_source") != "stream":
            for _, _, _, _, intent in reversed(signals(self.tape, now - SIGNAL_TTL, now, "sell", slug)):
                if (intent.get("side") == side and intent.get("exit_kind") in ("take_profit", "pre_news")
                        and intent["ready_at"] <= now < intent["deadline"] and best_bid is not None
                        and best_bid >= decimal(intent["limit"])):
                    trigger, discretionary_limit = "model_exit", str(intent["limit"])
                    self.store.set_trade(trade["id"], now, {"exit_reason": trigger, "exit_limit": discretionary_limit})
                    break
        if pending:
            if pending["kind"] == "target" and trigger:
                await self.cancel(pending)
            return
        if exits and now - exits[-1]["request"]["prepared_at"] < 2:
            return
        positions, working = await asyncio.gather(self.venue.positions(), self.venue.open_orders())
        if positions.get(slug, ZERO) != (remaining if side == "UP" else -remaining):
            raise VenueError("Exchange position differs from the confirmed order fills")
        if any(order.slug == slug for order in working):
            raise VenueError("Exchange has an unexpected working order on the Oil D position")
        if self.clock() + 1 >= cutoff or remaining < step or floor_to(remaining, step) != remaining:
            return
        if self.book_mode != "rest":
            book = self.exit_books.exit_snapshot(slug)
            if book is None:
                return
            bids = book.side_bids(side)
            best_bid = decimal(bids[0][0]) if bids else None
        if not usable(book, self.clock()):
            return
        if trigger == "stop_loss":
            if best_bid is None:
                return
            kind, price, expires = "stop_loss", max(tick, best_bid - 2 * tick), None
        elif trigger == "model_exit":
            limit = decimal(discretionary_limit)
            if best_bid is None or best_bid < limit:
                return
            kind, price, expires = "model_exit", limit, None
        else:
            kind, price, expires = "target", target, cutoff
        request = self.request(slug, side, "sell", remaining, price, expires_at=expires)
        order = self.store.prepare_order(trade["id"], kind, uuid4().hex, self.clock(), request)
        if usable(book, self.clock()) and self.clock() < cutoff:
            await self.submit(order)
        else:
            self.store.update_order(order["id"], "ORDER_STATE_CANCELED", self.clock(),
                                    payload={"reason": "The exit quote expired before submission."})

    def close(self, trade, entry, exits, payout, reason):
        orders = [entry, *exits]
        fees_exact = all(not self.filled(o) or o["payload"].get("fees") is not None for o in orders)
        reserve_rate = decimal(entry["request"]["fee_per_contract"])
        fees = sum((decimal(o["payload"]["fees"]) if o["payload"].get("fees") is not None else
                    self.filled(o) * reserve_rate + FEE_RESERVE for o in orders if self.filled(o)), ZERO)
        proceeds = sum((self.gross(o) for o in exits), payout)
        net = proceeds - self.gross(entry) - fees
        self.store.close_trade(trade["id"], self.clock(), "CLOSED", {"net_pnl": str(net), "fees": str(fees),
                               "fees_exact": fees_exact, "payout": str(payout), "exit_reason": reason,
                               "signal_at": entry["request"]["signal_at"]})
        self.status("waiting", "The Oil D position closed and the result was recorded.")


async def serve():
    import fcntl

    directory = WTI.directory()
    path = live_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await run(directory, LiveStore(path))


async def run(directory, store):
    from .oil_d_paper import run_oil_d_paper
    from .oil_d_shadow import run_oil_d_shadow
    from .replay_capture import StreamBookSource, run_shadow_stream

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    key = os.getenv("WTI_LIVE_KEY_ID", "").strip()
    secret = os.getenv("WTI_LIVE_SECRET_KEY", "").strip()
    enable = os.getenv("WTI_LIVE_ENABLED", "true").lower() == "true"
    shadow_enabled = os.getenv("WTI_OIL_D_SHADOW_ENABLED", "true").strip().lower() == "true"
    paper_enabled = os.getenv("WTI_OIL_D_PAPER_ENABLED", "true").strip().lower() == "true"
    book_mode = os.getenv("WTI_LIVE_BOOK_MODE", "rest").strip().lower()
    if book_mode not in ("rest", "stream_first", STREAM_MODE):
        LOG.warning("Unrecognized WTI_LIVE_BOOK_MODE. New entries disabled. Using REST for open positions.")
        book_mode = "rest"
        enable = False
    stream = StreamBookSource() if paper_enabled or shadow_enabled or book_mode != "rest" else None
    budget = RequestBudget.from_env()
    async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as private, \
            httpx.AsyncClient(base_url=BASE, timeout=2.0) as public:
        pilot = None
        message = "Live credentials are not configured."
        if key and secret:
            try:
                amount = configured_budget()
                venue = LiveVenue(private, key, secret)
                feed = Feed(ReadOnlyVenueClient(public, min_interval=.2, max_retries=0), public)
                pilot = Pilot(store, venue, feed, directory, amount, entries_allowed=enable,
                              stream_snapshot=stream.snapshot if book_mode != "rest" else None, book_mode=book_mode)
            except (ValueError, ArithmeticError, VenueError, StopIteration):
                message = "Live configuration is invalid. Check the credentials and pilot budget."
        if pilot:
            store.event("started", time.time(), {"title": "Oil D live worker started",
                        "message": "Checking the live account before processing new Oil D signals."})
            store.heartbeat(time.time(), {"worker_configured": True, "entries_configured": enable,
                                         "state": "starting", "message": "Checking the live account."})
        tasks = [asyncio.create_task(run_shadow_stream(directory, stop, credentials=(key, secret) if key and secret else None,
                                                       quote_source=stream))]
        if shadow_enabled:
            tasks.append(asyncio.create_task(run_oil_d_shadow(directory, stop, stream.snapshot)))
        if paper_enabled:
            tasks.append(asyncio.create_task(run_oil_d_paper(directory, stop, stream.snapshot, live_journal=store.path)))
        if pilot and enable:
            tasks.append(asyncio.create_task(prepare_entry_contracts(pilot, stop)))
        if budget is not None:
            tasks.append(asyncio.create_task(keep_request_priority(budget, store, pilot is not None and enable, stop)))
        try:
            await run_pilot(store, pilot, enable, message, stop)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def keep_request_priority(budget, store, configured, stop):
    last_mode, renewed_at = None, float("-inf")
    while not stop.is_set():
        try:
            mode = await asyncio.to_thread(request_priority_mode, store, configured)
            now = time.monotonic()
            if mode != last_mode or now - renewed_at >= 10:
                await asyncio.to_thread(budget.set_live_mode, *mode)
                last_mode, renewed_at = mode, now
        except sqlite3.Error:
            LOG.warning("Could not renew the Oil D public request priority lease", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass


def request_priority_mode(store, configured):
    return store.enabled(configured), store.active_trade() is not None


async def prepare_entry_contracts(pilot, stop):
    from .replay_capture import current_window

    while not stop.is_set():
        try:
            if pilot.store.enabled() and pilot.store.active_trade() is None:
                window = await asyncio.to_thread(current_window, pilot.tape, pilot.clock())
                if window is not None and pilot.clock() < window.end - pilot.settings.entry_before_end_seconds:
                    await pilot.entry_markets.prime(window)
        except (VenueError, TimeoutError, sqlite3.Error, OSError) as exc:
            LOG.debug("Oil D contract precheck unavailable: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass


async def run_pilot(store, pilot, enable, message, stop):
    last_block = None
    while not stop.is_set():
        if pilot:
            try:
                await pilot.tick()
            except (VenueError, FeedError, PublicRequestDeadline, httpx.HTTPError, sqlite3.Error,
                    ValueError, ArithmeticError, KeyError) as exc:
                message = str(exc) if isinstance(exc, (VenueError, FeedError, PublicRequestDeadline)) else type(exc).__name__
                pilot.status("blocked", message)
            block = pilot.report.get("message") if pilot.report.get("state") == "blocked" else None
            if block and block != last_block:
                store.event("blocked", time.time(), {"title": "Live execution blocked", "message": block})
                LOG.warning("Oil D live execution blocked: %s", block)
            last_block = block
            store.heartbeat(time.time(), {"worker_configured": True, "entries_configured": enable, **pilot.report,
                                          "entry_contract": pilot.entry_markets.status()})
        else:
            store.heartbeat(time.time(), {"worker_configured": False, "state": "unconfigured", "message": message})
        try:
            await asyncio.wait_for(stop.wait(), timeout=1 if pilot else 5)
        except TimeoutError:
            pass


async def begin_pilot(store, venue, pilot_key, budget, *, configured_cap, clock=time.time):
    """Verify flat account funds before committing a new paused pilot baseline."""
    existing = store.pilot_start_ready(pilot_key, budget, clock())
    if existing is not None:
        return existing, False
    if budget > configured_cap:
        raise ValueError("The requested pilot budget exceeds this worker's configured cap.")
    checks = [asyncio.create_task(check) for check in (venue.positions(), venue.open_orders(), venue.buying_power())]
    try:
        async with asyncio.timeout(8):
            positions, orders, buying_power = await asyncio.gather(*checks)
    finally:
        for check in checks:
            check.cancel()
        await asyncio.gather(*checks, return_exceptions=True)
    if any(positions.values()) or orders:
        raise ValueError("The exchange account has an open position or working order. Leave Oil D paused until flat.")
    return store.start_pilot(pilot_key, budget, clock(), buying_power)


async def begin_pilot_on_vm(pilot_key, budget):
    path = live_path(WTI.directory())
    if not path.is_file():
        raise ValueError("The Oil D live journal is missing.")
    key, secret = (os.getenv(name, "").strip() for name in ("WTI_LIVE_KEY_ID", "WTI_LIVE_SECRET_KEY"))
    if not key or not secret:
        raise ValueError("The live worker has no Polymarket US credentials configured.")
    amount = configured_budget()
    async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as private:
        return await begin_pilot(LiveStore(path), LiveVenue(private, key, secret), pilot_key, budget,
                                 configured_cap=amount)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if sys.argv[1:] == ["--resume-operator-pause"]:
        if not LiveStore(live_path(WTI.directory())).resume_operator_pause(time.time()):
            raise SystemExit("Live entries were not resumed. Check the operator pause, flat position, and current pilot heartbeat.")
        print("Oil D live entries resumed.")
    elif sys.argv[1:2] == ["--begin-pilot"]:
        parser = argparse.ArgumentParser(description="Configure a funded Oil D pilot while entries are paused.")
        parser.add_argument("--begin-pilot", required=True, metavar="KEY")
        parser.add_argument("--budget-usd", required=True)
        args = parser.parse_args()
        try:
            pilot, created = asyncio.run(begin_pilot_on_vm(args.begin_pilot, decimal(args.budget_usd)))
        except (ValueError, ArithmeticError, VenueError, TimeoutError, OSError, sqlite3.Error) as exc:
            detail = "Account verification timed out." if isinstance(exc, TimeoutError) else str(exc)
            raise SystemExit(f"Oil D pilot was not started. {detail}") from None
        if created:
            print(f"Pilot {pilot['pilot_key']} configured with ${Decimal(pilot['starting_budget']):.2f}. "
                  "Live entries are still paused.")
        else:
            print(f"Pilot {pilot['pilot_key']} already exists. Its budget and baseline are unchanged.")
    elif len(sys.argv) == 1:
        asyncio.run(serve())
    else:
        raise SystemExit("Usage: python -m backend.wti.live_worker "
                         "[--resume-operator-pause | --begin-pilot KEY --budget-usd AMOUNT]")
