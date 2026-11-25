"""Run Oil D 15m with a separate cash ledger and modeled exchange execution."""

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
import sqlite3
import time

import httpx

from ..trading.venue import BASE, ReadOnlyVenueClient
from ..updown.feed import Feed
from ..updown.strategy import wti_products
from .commodity import WTI
from .live_books import BookUnavailable, EXIT_BUFFER, LiveBooks, TAPE_POLL_INTERVAL
from .oil_d_paper_store import PAPER_ENTRY_CAP, PAPER_LOSS_CAP, STARTING_BUDGET, ZERO, PaperAccount, paper_path, report
from .oil_d_shadow_exits import SETTLEMENT_INTERVAL, official_payout
from .oil_d_shadow_strategy import FIXED_FEE_RESERVE, MIN_FEE_RESERVE, simulate_arrival
from .oil_d_stream_signals import OilDStreamSignals
from .paper_execution import PaperExecution, PaperExits
from .tape import Tape, tape_path


LOG = logging.getLogger(__name__)
ENTRY_TRANSPORT_SECONDS = 0.15
SCAN_SECONDS = 5.0
ACTIVE_SECONDS = 0.25
SETTLING_SECONDS = 5.0
HEARTBEAT_SECONDS = 5.0


def _amount(value):
    return Decimal(str(value))


class OilDPaper:
    def __init__(self, directory, store, snapshot, settlement, clock=time.time):
        self.store, self.snapshot, self.settlement, self.clock = store, snapshot, settlement, clock
        self.signal_source = OilDStreamSignals(directory, snapshot, clock)
        self.exit_books = LiveBooks(tape_path(directory), None, clock, stream_snapshot=snapshot)
        self.pending = None
        self.last_rejection = None
        self.sent = None
        self.exit_engine = None
        self.exit_slug = None
        self.delay = SCAN_SECONDS
        self.last_heartbeat = float("-inf")
        self.last_gate = None

    async def tick(self):
        gate = await asyncio.to_thread(self.store.entry_gate)
        await self._tick(gate)
        now = self.clock()
        if gate != self.last_gate or now - self.last_heartbeat >= HEARTBEAT_SECONDS:
            gate = await asyncio.to_thread(self.store.entry_gate)
            await asyncio.to_thread(self.store.heartbeat, now, *gate)
            self.last_gate, self.last_heartbeat = gate, now

    async def _tick(self, gate):
        active = await asyncio.to_thread(self.store.active)
        if active is not None:
            self.pending = None
            if active["state"] == "sent":
                if gate[0]:
                    await self.entry_arrival(active)
                else:
                    data = active["payload"]
                    await asyncio.to_thread(self.store.update, active, self.clock(), "no_fill",
                                            {**data, "entry": {"outcome": "canceled", "reason": gate[1],
                                                               "filled_quantity": ZERO}})
                    self.sent = None
                    self.delay = SCAN_SECONDS
            else:
                await self.manage_exits(active)
            return
        self.exit_engine = None
        self.exit_slug = None
        self.delay = SCAN_SECONDS
        realized = await asyncio.to_thread(self.store.realized)
        if not gate[0] or realized <= -PAPER_LOSS_CAP or STARTING_BUDGET + realized <= ZERO:
            self.pending = None
            return
        if self.pending is not None:
            self.delay = TAPE_POLL_INTERVAL
            await self.try_entry(realized)
            return
        window, inputs = await asyncio.to_thread(self.signal_source.reference, self.clock())
        if window is None or await asyncio.to_thread(self.store.used, window.start):
            return
        result = self.signal_source.evaluate(window, inputs)
        if result.intent is not None:
            self.pending = window, result.intent
            self.last_rejection = None
            self.delay = TAPE_POLL_INTERVAL

    async def skip(self, window, intent, reason):
        await asyncio.to_thread(self.store.skip, window.slug, intent.signal_at, self.clock(), reason)
        self.pending = None

    async def try_entry(self, realized):
        window, intent = self.pending
        now = self.clock()
        if now < intent.ready_at:
            return
        if now >= intent.deadline:
            await self.skip(window, intent, self.last_rejection or "signal_expired")
            return
        terms, inputs = await asyncio.to_thread(self.signal_source.reference, now)
        now = self.clock()
        if (not self.signal_source.is_entry_window(terms, now)
                or (terms.slug, terms.start, terms.end, terms.price_to_beat, terms.price_tick,
                    terms.minimum_quantity, terms.theta) != (window.slug, window.start, window.end, window.price_to_beat,
                    window.price_tick, window.minimum_quantity, window.theta)):
            await self.skip(window, intent, "contract_changed")
            return
        if not self.signal_source.inputs_are_current(inputs, now):
            await self.skip(window, intent, "index_unavailable")
            return
        if self.signal_source.entry_hold(terms, inputs, now):
            await self.skip(window, intent, "news_or_shock_hold")
            return
        book, halted = self.snapshot(window.slug)
        result = simulate_arrival(intent, book, now=now, minimum_quantity=window.minimum_quantity,
                                  price_tick=window.price_tick, buying_power=STARTING_BUDGET + realized,
                                  pilot_budget=STARTING_BUDGET, net_pnl=realized, market_open=not halted,
                                  entry_cap=PAPER_ENTRY_CAP, loss_cap=PAPER_LOSS_CAP)
        if result.fill is None:
            self.last_rejection = result.reason
            if halted or result.reason in ("budget_below_minimum", "loss_cap_reached", "invalid_contract_terms"):
                await self.skip(window, intent, result.reason)
            return
        filled = result.fill
        settings = self.signal_source.settings
        reserved = filled.quantity * (filled.limit + max(MIN_FEE_RESERVE, intent.theta / 4)) + FIXED_FEE_RESERVE
        payload = {"side": intent.side, "limit": filled.limit, "quantity": filled.quantity, "ready_at": intent.ready_at,
                   "quote_at_send": book.at, "signal_deadline": intent.deadline,
                   "reserved_amount": reserved,
                   "minimum_quantity": window.minimum_quantity, "price_tick": window.price_tick,
                   "fee_coefficient": intent.theta, "take_profit_fraction": settings.take_profit_target or .5,
                   "stop_loss_fraction": settings.stop_loss or .5, "arrival_due": now + ENTRY_TRANSPORT_SECONDS}
        if await asyncio.to_thread(self.store.send, window, intent.signal_at, now, payload):
            self.sent = window.slug
            self.delay = ENTRY_TRANSPORT_SECONDS
        self.pending = None

    async def entry_arrival(self, trade):
        now = self.clock()
        data = trade["payload"]
        self.delay = max(.01, data["arrival_due"] - now)
        if self.sent != trade["slug"]:
            await asyncio.to_thread(self.store.update, trade, now, "unverified",
                                    {**data, "entry": {"outcome": "unverified", "reason": "interrupted_before_arrival"}})
            return
        if now < data["arrival_due"]:
            return
        book, halted = self.snapshot(trade["slug"])
        now = self.clock()
        engine = PaperExecution()
        window_cutoff = trade["window_end"] - EXIT_BUFFER
        cutoff = min(data.get("signal_deadline", window_cutoff), window_cutoff)
        match = engine.match_ioc(book, side=data["side"], action="buy", quantity=_amount(data["quantity"]),
                                 limit=_amount(data["limit"]), arrival_at=now, cutoff=cutoff,
                                 quantity_step=_amount(data["minimum_quantity"]), fee_coefficient=_amount(data["fee_coefficient"]),
                                 market_open=not halted, minimum_quote_at=data["quote_at_send"])
        updated = {**data, "entry": asdict(match)}
        state = "unverified" if match.outcome == "unverified" else "no_fill"
        if match.filled_quantity:
            updated.update(execution=engine.dump(), remaining=match.filled_quantity)
            state = "open"
        await asyncio.to_thread(self.store.update, trade, now, state, updated)
        self.sent = None
        self.delay = ACTIVE_SECONDS

    @staticmethod
    def restore_exits(trade):
        data = trade["payload"]
        entry = data["entry"]
        engine = PaperExecution(state=data["execution"])
        return PaperExits(engine, side=data["side"], quantity=_amount(entry["filled_quantity"]),
                          entry_price=_amount(entry["average_price"]), entry_at=entry["arrival_at"],
                          window_end=trade["window_end"], quantity_step=_amount(data["minimum_quantity"]),
                          price_tick=_amount(data["price_tick"]), fee_coefficient=_amount(data["fee_coefficient"]),
                          take_profit_fraction=_amount(data["take_profit_fraction"]),
                          stop_loss_fraction=_amount(data["stop_loss_fraction"]), state=data.get("exit_state"))

    async def manage_exits(self, trade):
        slug = trade["slug"]
        if self.exit_slug != slug:
            self.exit_engine = self.restore_exits(trade)
            self.exit_slug = slug
        exits = self.exit_engine
        now = self.clock()
        payout = None
        book, market_open = None, True
        if now >= trade["window_end"]:
            if exits.remaining:
                payout = await self.settlement(slug, trade["window_start"], trade["window_end"], now)
        else:
            try:
                book = await asyncio.to_thread(self.exit_books.exit_snapshot, slug)
            except BookUnavailable:
                market_open = False
        now = self.clock()
        events = exits.advance(now, book, market_open=market_open, official_yes_payout=payout)
        state = "settling" if now >= trade["window_end"] else "open"
        if not exits.remaining:
            state = "closed"
        self.delay = SETTLING_SECONDS if state == "settling" else ACTIVE_SECONDS
        next_action = exits.next_action_at
        if state == "open" and next_action is not None and next_action > now:
            self.delay = min(self.delay, max(.01, next_action - now))
        data = trade["payload"]
        if not events and state == trade["state"] and exits.coverage_reason == data.get("coverage"):
            return
        proceeds = exits.gross - exits.fees + exits.settled_proceeds
        updated = {**data, "execution": exits.execution.dump(), "exit_state": exits.dump(),
                   "exit_events": [*data.get("exit_events", ()), *(asdict(event) for event in events)],
                   "remaining": exits.remaining, "coverage": exits.coverage_reason, "net_exit_proceeds": proceeds}
        if state == "closed":
            updated["net_pnl"] = proceeds - _amount(data["entry"]["gross"]) - _amount(data["entry"]["fee"])
        if not await asyncio.to_thread(self.store.update, trade, now, state, updated):
            self.exit_slug = None


async def run_oil_d_paper(directory, stop, snapshot, *, live_journal):
    """Use the shared WTI stream and only budgeted public requests for missing payouts."""
    account = None
    next_lookup = float("-inf")
    tape = tape_path(directory)
    product, = wti_products(("15m",))
    async with httpx.AsyncClient(base_url=BASE, timeout=3) as client:
        feed = None

        async def settlement(slug, start, end, now):
            nonlocal feed, next_lookup
            if not tape.is_file():
                return None
            payout = await asyncio.to_thread(official_payout, tape, slug, start, end, now)
            if payout is not None or time.monotonic() < next_lookup:
                return payout
            next_lookup = time.monotonic() + SETTLEMENT_INTERVAL
            if feed is None:
                feed = Feed(ReadOnlyVenueClient(client, min_interval=.2, max_retries=0), client)
            try:
                result = await asyncio.wait_for(feed.settlement(product, start), timeout=4)
            except (TimeoutError, httpx.HTTPError, KeyError, TypeError, ValueError):
                return None
            if result is None or result.slug != slug:
                return None
            recorded = await asyncio.to_thread(Tape, tape)
            await asyncio.to_thread(recorded.record_settlement, result, time.time())
            return _amount(result.yes_payout)

        last_error = None
        while not stop.is_set():
            try:
                if account is None:
                    store = await asyncio.to_thread(PaperAccount, paper_path(directory), time.time(),
                                                    live_journal=live_journal)
                    account = OilDPaper(directory, store, snapshot, settlement)
                await account.tick()
                delay, last_error = account.delay, None
            except (OSError, sqlite3.Error, ValueError, ArithmeticError, TypeError, KeyError) as exc:
                if account is not None:
                    account.exit_slug = None
                    try:
                        await asyncio.to_thread(account.store.heartbeat, time.time(), False, "worker_error", error=True)
                    except (OSError, sqlite3.Error):
                        pass
                    account.last_gate = None
                error = type(exc).__name__
                if error != last_error:
                    LOG.warning("Oil D paper execution skipped a check (%s)", error, exc_info=True)
                delay, last_error = SCAN_SECONDS, error
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--journal", type=Path, default=paper_path(WTI.directory()))
    parser.add_argument("--json", action="store_true", help="include all orders and transitions in the period")
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")
    now = time.time()
    result = report(args.journal, now - args.hours * 3600, now, detail=args.json)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
        return
    print(f"Oil D 15m execution paper | {args.hours}h | PAPER ONLY")
    if result["started_at"] is None:
        print("Waiting for the Oil D execution paper account to start.")
        return
    started = datetime.fromtimestamp(result["started_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"Started {started} | ${result['starting_budget']:g} starting budget | ${result['entry_cap']:g} entry cap | "
          f"${result['loss_cap']:g} realized loss limit")
    print(f"Balance ${result['balance']:.2f} | buying power ${result['buying_power']:.2f} | "
          f"realized ${result['realized_pnl']:+.2f} | loss guard {'stopped' if result['loss_guard'] else 'ready'}")
    if result["last_heartbeat"]:
        state = result["worker_state"]
        gate = "allowed" if result["entries_allowed"] else "blocked"
        print(f"Worker {state} | new entries {gate} | last check {result['last_heartbeat']}")
    print(f"Period P&L ${result['period_net']:+.2f} on {result['closed_in_period']} closed trades | "
          f"quote gaps affected {result['closed_with_coverage_gap']} trades")
    for label, key in (("Entry outcomes", "entry_outcomes"), ("Entry checks skipped", "skip_reasons")):
        if result[key]:
            print(label + " | " + ", ".join(f"{name} {count}" for name, count in result[key].items()))
    if result["active"]:
        print("Active | " + " | ".join(str(value) for value in result["active"].values() if value is not None))


if __name__ == "__main__":
    main()
