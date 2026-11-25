"""Evaluate Oil D on the current WTI index inputs and a WebSocket quote."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

from ..updown.strategy import Book, Decision
from . import Rules
from .commodity import WTI
from .events import Calendar
from .live_books import ENTRY_EXCHANGE_MAX_AGE, usable
from .models import ModelSet
from .oil_d_shadow_inputs import read_shadow_inputs
from .oil_d_shadow_strategy import ShadowIntent, signal_for_quote
from .replay_capture import current_window
from .tape import tape_path


SIGNAL_BANKROLL = 100.0


@dataclass(frozen=True)
class StreamEvaluation:
    at: float
    reason: str
    intent: ShadowIntent | None = None
    decision: Decision | None = None
    book: Book | None = None
    gate: str | None = None
    fair_up: float | None = None


class OilDStreamSignals:
    """Read current reference data and produce a signal without submitting or simulating a fill."""

    def __init__(self, directory, snapshot, clock=time.time, *, settings=None):
        self.directory, self.snapshot, self.clock = directory, snapshot, clock
        self.tape = tape_path(directory)
        self.settings = settings or WTI.settings()
        calendar = Calendar(directory / "events.json", WTI.recurring, WTI.jumps)
        self.rules = Rules.for_commodity(WTI, calendar, None)
        self.model = ModelSet(calendar, None)

    def reference(self, at):
        window = current_window(self.tape, at)
        return window, read_shadow_inputs(self.directory) if window is not None else None

    def is_entry_window(self, window, now):
        return (window is not None and window.key == "wti-15m" and window.end - window.start == 900
                and window.status == "MARKET_STATUS_OPEN"
                and window.start + self.settings.entry_after_seconds <= now
                < window.end - self.settings.entry_before_end_seconds)

    def inputs_are_current(self, inputs, now):
        return (inputs is not None and 0 <= now - inputs.published_at <= self.settings.max_index_age
                and 0 <= now - inputs.index_at <= self.settings.max_index_age)

    def entry_hold(self, window, inputs, now):
        return self.rules.entry_hold(window, now, shock=inputs.last_shock_at, roll=inputs.last_roll_at)

    def evaluate(self, window, inputs):
        now = self.clock()
        if not self.is_entry_window(window, now):
            return StreamEvaluation(now, "outside_entry_window")
        if not self.inputs_are_current(inputs, now):
            return StreamEvaluation(now, "index_unavailable")
        hold = self.entry_hold(window, inputs, now)
        if hold:
            return StreamEvaluation(now, "news_or_shock_hold", gate=hold)
        if not (window.price_to_beat and window.minimum_quantity and window.price_tick):
            return StreamEvaluation(now, "contract_terms_unavailable")
        book, halted = self.snapshot(window.slug)
        if halted:
            return StreamEvaluation(now, "market_not_open")
        if book is None or not usable(book, now, exchange_max_age=ENTRY_EXCHANGE_MAX_AGE):
            return StreamEvaluation(now, "fresh_stream_unavailable")
        try:
            fair_up = self.model.model_d(window.price_to_beat, inputs.index_price, window.end - now,
                                         inputs.minute_closes, book, now, window.end)
            if not math.isfinite(fair_up):
                raise ValueError("Invalid fair value")
        except (ValueError, ArithmeticError):
            return StreamEvaluation(now, "model_inputs_unavailable")
        decision = signal_for_quote(window, book, now=now, fair_up=fair_up, settings=self.settings,
                                    balance=SIGNAL_BANKROLL, open_stake=0.0, today_pnl=0.0,
                                    minimum_quantity=window.minimum_quantity)
        return StreamEvaluation(now, decision.reason, decision.intent, decision.decision, book, fair_up=fair_up)
