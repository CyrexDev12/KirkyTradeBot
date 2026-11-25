"""Forward-only simulation of Oil D's 15-minute signal and entry using one WebSocket feed."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
import sqlite3
import time

from ..updown.model import taker_fee
from ..updown.strategy import Window
from .live_books import ENTRY_EXCHANGE_MAX_AGE, TAPE_POLL_INTERVAL, usable
from .live_timing import readonly
from .oil_d_shadow_exits import forward_context, run_forward_shadow_exits
from .oil_d_shadow_inputs import ShadowInputs
from .oil_d_shadow_store import OilDShadowStore, oil_d_shadow_path
from .oil_d_shadow_strategy import ENTRY_CAP, ShadowIntent, simulate_arrival
from .oil_d_stream_signals import OilDStreamSignals

LOG = logging.getLogger(__name__)
SCAN_INTERVAL = 5.0
LEGACY_MATCH_SECONDS = 15.0
PILOT_BUDGET = 12.0


def stream_details(book, now, side, fair_up, theta):
    asks = book.side_asks(side) if side in ("UP", "DOWN") else ()
    best_ask = asks[0][0] if asks else None
    fair = fair_up if side == "UP" else 1 - fair_up
    return {
        "best_ask": best_ask,
        "net_edge": None if best_ask is None else fair - best_ask - taker_fee(best_ask, float(theta)),
        "book_received_at": book.at, "book_exchange_at": book.exchange_at, "book_sequence": book.sequence,
        "receipt_age_seconds": now - book.at, "exchange_age_seconds": now - book.exchange_at,
    }


@dataclass
class Pending:
    intent: ShadowIntent
    window: Window
    inputs: ShadowInputs
    original: dict
    last_quote: dict | None = None


class OilDShadow:
    def __init__(self, directory, store, snapshot, clock=time.time, *, settings=None):
        self.store, self.snapshot, self.clock = store, snapshot, clock
        self.signal_source = OilDStreamSignals(directory, snapshot, clock, settings=settings)
        self.tape, self.settings = self.signal_source.tape, self.signal_source.settings
        self.pending = None
        self.slug = None
        self.filled = False
        self.last_evaluation = None

    def reference(self, at):
        window, inputs = self.signal_source.reference(at)
        if window is None:
            return None, None, {}
        with readonly(self.tape) as db:
            row = db.execute("""
                SELECT at,payload FROM tape WHERE slug=? AND source='intent' AND kind='order'
                AND at BETWEEN ? AND ? AND json_extract(payload,'$.model')='D'
                AND json_extract(payload,'$.action')='buy' ORDER BY at DESC,id DESC LIMIT 1
            """, (window.slug, at - LEGACY_MATCH_SECONDS, at)).fetchone()
        if row is None:
            return window, inputs, {}
        payload = json.loads(row[1])
        return window, inputs, {
            "original_signal_at": row[0], "original_limit": payload.get("limit"),
            "original_side": payload.get("side"), "original_fair_up": payload.get("fair_up"),
            "original_min_edge": payload.get("min_edge"), "original_theta": payload.get("theta"),
        }

    def evidence(self, book, now, side, fair_up, theta, original):
        result = stream_details(book, now, side, fair_up, theta)
        if original.get("original_side") in ("UP", "DOWN") and original.get("original_fair_up") is not None:
            legacy = stream_details(book, now, original["original_side"], float(original["original_fair_up"]),
                                    float(original.get("original_theta") or theta))
            result.update(original_best_ask=legacy["best_ask"], original_net_edge=legacy["net_edge"])
            if legacy["best_ask"] is not None and original.get("original_limit") is not None:
                result["ask_delta_cents"] = round(100 * (legacy["best_ask"] - float(original["original_limit"])), 4)
        return result

    async def record(self, slug, signal_at, stage, at, outcome, reason, **details):
        await asyncio.to_thread(self.store.record, slug, signal_at, stage, at, {
            "outcome": outcome, "reason": reason, "source": "stream", "entry_cap_usd": ENTRY_CAP,
            "max_exchange_age_seconds": ENTRY_EXCHANGE_MAX_AGE, **details,
        })

    async def evaluation(self, window, at, reason, **details):
        # Record the latest skipped evaluation per contract. Signal and execution rows keep their own history.
        key = reason, details.get("original_signal_at")
        if self.last_evaluation != key:
            await self.record(window.slug, window.start, "evaluation", at, "skipped", reason, **details)
            self.last_evaluation = key

    async def scan(self):
        window, inputs, original = await asyncio.to_thread(self.reference, self.clock())
        if not self.signal_source.is_entry_window(window, self.clock()):
            return
        if self.slug != window.slug:
            self.filled = await asyncio.to_thread(self.store.has_fill, window.slug)
            self.slug = window.slug
            self.last_evaluation = None
        if self.filled:
            return
        decision = self.signal_source.evaluate(window, inputs)
        now, book, fair_up = decision.at, decision.book, decision.fair_up
        if book is None:
            if decision.reason != "outside_entry_window":
                details = {**original, **({"gate": decision.gate} if decision.gate else {})}
                await self.evaluation(window, now, decision.reason, **details)
            return
        side = decision.intent.side if decision.intent else decision.decision.side if decision.decision else None
        details = self.evidence(book, now, side, fair_up, window.theta, original)
        details.update(side=side, fair_up=fair_up, min_edge=self.settings.min_edge,
                       index_at=inputs.index_at, index_price=inputs.index_price, **original)
        if decision.intent is None:
            await self.evaluation(window, now, decision.reason, **details)
            return
        intent = decision.intent
        await self.record(window.slug, intent.signal_at, "signal", now, "signaled", decision.reason, **details,
                          shadow_limit=intent.limit, ready_at=intent.ready_at, deadline=intent.deadline)
        self.pending = Pending(intent, window, inputs, original)

    async def execute(self):
        pending = self.pending
        intent, window = pending.intent, pending.window
        now = self.clock()
        if now < intent.ready_at:
            return
        if now >= intent.deadline:
            last = pending.last_quote or {}
            reason = last.get("reason", "fresh_stream_unavailable")
            await self.finish(pending, now, "skipped", reason, last)
            return
        book, halted = self.snapshot(intent.slug)
        if halted:
            await self.finish(pending, now, "skipped", "market_not_open", pending.last_quote)
            return
        hold = self.signal_source.entry_hold(window, pending.inputs, now)
        if hold:
            await self.finish(pending, now, "skipped", "news_or_shock_hold", {"gate": hold})
            return
        if book is None or not usable(book, now, intent.ready_at, exchange_max_age=ENTRY_EXCHANGE_MAX_AGE):
            return
        execution = simulate_arrival(intent, book, now=now, minimum_quantity=window.minimum_quantity,
                                     price_tick=window.price_tick, buying_power=PILOT_BUDGET, pilot_budget=PILOT_BUDGET)
        details = self.evidence(book, now, intent.side, intent.fair_up, intent.theta, pending.original)
        details.update(eligible_depth=execution.eligible_depth, required_depth=execution.required_depth,
                       eligible_quantity=execution.eligible_quantity, reason=execution.reason)
        if execution.fill is None:
            pending.last_quote = details
            return
        fill = asdict(execution.fill)
        await self.finish(pending, now, "filled", execution.reason, {**details, "fill": fill, "quantity": fill["quantity"]})
        self.filled = True

    async def finish(self, pending, now, outcome, reason, details):
        intent = pending.intent
        data = {**pending.original, **(details or {})}
        data.pop("reason", None)
        if outcome == "filled":
            data["forward_exits"] = forward_context(pending.window, self.settings, intent.theta)
        await self.record(intent.slug, intent.signal_at, "execution", now, outcome, reason, **data,
                          side=intent.side, shadow_limit=intent.limit, fair_up=intent.fair_up, min_edge=intent.min_edge,
                          ready_at=intent.ready_at, deadline=intent.deadline)
        self.pending = None


async def run_oil_d_shadow(directory, stop, snapshot):
    exits = asyncio.create_task(run_forward_shadow_exits(directory, stop))
    try:
        await _run_entries(directory, stop, snapshot)
    finally:
        exits.cancel()
        await asyncio.gather(exits, return_exceptions=True)


async def _run_entries(directory, stop, snapshot):
    shadow = None
    while not stop.is_set():
        try:
            if shadow is None:
                store = await asyncio.to_thread(OilDShadowStore, oil_d_shadow_path(directory))
                shadow = OilDShadow(directory, store, snapshot)
            if shadow.pending is None:
                await shadow.scan()
            else:
                await shadow.execute()
        except (OSError, sqlite3.Error, ValueError, ArithmeticError) as exc:
            LOG.warning("Oil D shadow data unavailable (%s)", type(exc).__name__)
        delay = TAPE_POLL_INTERVAL if shadow is not None and shadow.pending is not None else SCAN_INTERVAL
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
