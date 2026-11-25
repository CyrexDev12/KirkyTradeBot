"""Replay the exact order sizes and limits journaled for Oil D's live WTI pilot."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from math import isfinite
from pathlib import Path
import sqlite3
import time

from .commodity import WTI
from .entry_replay import get_frames
from .live_books import BOOK_MAX_AGE, ENTRY_EXCHANGE_MAX_AGE, EXCHANGE_MAX_AGE, usable
from .live_store import TERMINAL_STATES, live_path
from .live_timing import as_decimal, distribution, entry_exchange_age_limit, number, readonly
from .paper_execution import MAX_UNOBSERVED_SECONDS, TARGET_DEPTH_SHARE, PaperExecution
from .replay_capture import StreamBookSource
from .shadow import decode_window, recorded_book
from .tape import tape_path


ZERO, ONE = Decimal(0), Decimal(1)
DEFAULT_DELAY = .15
MODEL_OUTCOMES = frozenset(("filled", "partial", "no_fill", "expired"))
_ORDER_SOURCE = {"stream": "stream", "rest": "recorded_rest", "recorded_rest": "recorded_rest",
                 "direct_rest": "direct_rest"}


@dataclass(frozen=True)
class Order:
    order_id: int
    trade_id: int
    slug: str
    window_start: int
    window_end: int
    kind: str
    side: str
    limit: Decimal | None
    quantity: Decimal | None
    sent_at: float | None
    ready_at: float | None
    entry_exchange_age: float
    target_until: float | None
    cancel_requested_at: float | None
    source: str | None
    source_inferred: bool
    acknowledged: bool
    actual_state: str
    actual_quantity: Decimal | None
    actual_price: Decimal | None
    actual_fee: Decimal | None
    first_fill_at: float | None
    trade_net: Decimal | None
    trade_fee_exact: bool
    closed_at: float | None

    @property
    def resting(self):
        return self.kind == "target"


def _has(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _entry_timings(db, since, as_of):
    if not _has(db, "entry_timings"):
        return {}
    entries = {}
    for row in db.execute("SELECT payload FROM entry_timings WHERE at BETWEEN ? AND ? ORDER BY at,id",
                          (since - 900, as_of)):
        data = json.loads(row[0])
        sent = number(data.get("submit_started_at"))
        if sent is not None and sent <= as_of:
            entries[(data.get("slug"), number(data.get("signal_at")))] = data
    return entries


def _order_events(db, first_created, as_of, selected_ids):
    sends, latest, cancels = {}, {}, {}
    if not _has(db, "events"):
        return sends, latest, cancels
    for stamp, raw in db.execute("""SELECT at,payload FROM events WHERE kind='order_update'
                                   AND at BETWEEN ? AND ? ORDER BY at,id""", (first_created, as_of)):
        order = json.loads(raw).get("order") or {}
        oid, state = order.get("id"), order.get("state")
        if oid not in selected_ids:
            continue
        latest[oid] = order
        if state == "sending":
            sends.setdefault(oid, stamp)
        if state == "ORDER_STATE_CANCELED":
            cancels.setdefault(oid, stamp)
    return sends, latest, cancels


def _first_fills(db, first_created, as_of, selected_ids):
    result = {}
    if _has(db, "fills"):
        for row in db.execute("""SELECT order_id,MIN(json_extract(payload,'$.exchange_at')) AS stamp
                                FROM fills WHERE at BETWEEN ? AND ?
                                AND COALESCE(json_extract(payload,'$.observed_at'),at)<=? GROUP BY order_id""",
                              (first_created, as_of, as_of)):
            if row["order_id"] in selected_ids:
                result[row["order_id"]] = number(row["stamp"])
    return result


def load_orders(path, since, as_of, trade_ids=None):
    """Read order states as of the requested end, including zero fills and unresolved orders."""
    with readonly(Path(path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = db.execute("""SELECT o.*,t.slug,t.window_start,t.window_end,t.closed_at,t.payload AS trade_payload,
                            s.at AS signal_at,s.payload AS signal_payload
                            FROM orders o JOIN trades t ON t.id=o.trade_id JOIN signals s ON s.id=t.signal_id
                            WHERE o.created_at BETWEEN ? AND ? AND t.window_end - t.window_start=900
                            AND t.slug GLOB 'cpwtif-wti-updown-15m-*' ORDER BY o.created_at,o.id""",
                          (since - 900, as_of)).fetchall()
        rows = [row for row in rows if trade_ids is None or row["trade_id"] in trade_ids]
        if not rows:
            return []
        ids = {row["id"] for row in rows}
        first_created = rows[0]["created_at"]
        sends, event_versions, cancels = _order_events(db, first_created, as_of, ids)
        fills = _first_fills(db, first_created, as_of, ids)
        timings = _entry_timings(db, since, as_of)

    result = []
    for row in rows:
        oid = row["id"]
        version = row if row["updated_at"] <= as_of else event_versions.get(oid)
        request = json.loads(row["request"])
        payload = version["payload"] if version is not None else {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        signal = json.loads(row["signal_payload"])
        kind = row["kind"]
        timing = timings.get((row["slug"], row["signal_at"]), {})
        sent = number(timing.get("submit_started_at")) if kind == "entry" else None
        if sent is None:
            sent = sends.get(oid)
        if (sent if sent is not None else row["created_at"]) < since:
            continue
        mode = timing.get("book_mode")
        source = request.get("quote_source") or (timing.get("final_quote_source") if kind == "entry" else None)
        inferred = source is None and kind != "entry"
        if inferred:
            source = ("stream_first" if mode in ("stream_first", "stream_end_to_end")
                      or signal.get("signal_source") == "stream" else "recorded_rest")
        else:
            source = _ORDER_SOURCE.get(source)
        target_until = cancel_requested_at = None
        if kind == "target":
            expiry = number(request.get("expires_at"))
            cancel_requested_at = number(payload.get("cancel_requested_at"))
            if expiry is not None:
                target_until = min(value for value in (expiry, cancels.get(oid), row["window_end"]) if value is not None)
        venue_id = version["venue_id"] if version is not None else None
        state = version["state"] if version is not None else "unobserved"
        closed_at = number(row["closed_at"])
        trade_payload = json.loads(row["trade_payload"]) if closed_at is not None and closed_at <= as_of else {}
        actual_quantity = as_decimal(payload.get("filled")) if venue_id else None
        actual_price = as_decimal(payload.get("average_price")) if actual_quantity else None
        actual_fee = as_decimal(payload.get("fees")) if actual_quantity else ZERO if actual_quantity == ZERO else None
        entry_age = (ENTRY_EXCHANGE_MAX_AGE if source == "stream" and not timing else entry_exchange_age_limit(timing))
        result.append(Order(oid, row["trade_id"], row["slug"], row["window_start"], row["window_end"], kind,
                            request.get("side"), as_decimal(request.get("price")), as_decimal(request.get("quantity")),
                            sent, number(signal.get("ready_at")), entry_age, target_until, cancel_requested_at,
                            source, inferred, bool(venue_id), state,
                            actual_quantity, actual_price, actual_fee, fills.get(oid), as_decimal(trade_payload.get("net_pnl")),
                            trade_payload.get("fees_exact") is True, closed_at if trade_payload else None))
    return result


class QuoteCursor:
    """Combine timestamped quote sources without letting a future frame into a probe."""

    def __init__(self, window, frames):
        self.at = window.start
        self.window = window
        self.stream = StreamBookSource(clock=lambda: self.at)
        self.generation = self.stream.begin(window)
        self.stream.set_connected(self.generation, True)
        self.rest = None
        self.rest_closed = False
        self.events = sorted(
            [(at, source, kind, payload) for source, rows in frames.items() for at, kind, payload in rows],
            key=lambda row: row[0],
        )
        self.cursor = iter(self.events)
        self.following = next(self.cursor, None)

    def select(self, at, source, exchange_age, ready_at=None):
        while self.following is not None and self.following[0] <= at:
            self.at, kind_source, kind, payload = self.following
            if kind_source == "stream":
                self.stream.observe(self.generation, (self.window.slug, self.at, kind_source, kind, payload))
            else:
                self.rest = recorded_book(self.at, payload) if kind == "book" else None
                if kind == "closed":
                    self.rest_closed = True
                elif self.rest is not None:
                    self.rest_closed = False
            self.following = next(self.cursor, None)
        self.at = at
        if source in ("stream", "stream_first"):
            book, halted = self.stream.snapshot(self.window.slug)
            if halted or (book is not None and usable(book, at, ready_at, exchange_max_age=exchange_age)):
                return book, halted, "stream"
            if source == "stream":
                return None, False, "stream"
        if self.rest is not None and not usable(self.rest, at, ready_at, exchange_max_age=exchange_age):
            return None, False, "recorded_rest"
        return self.rest, self.rest_closed, "recorded_rest"


def _actual_outcome(order):
    if not order.acknowledged:
        return "unacknowledged"
    if order.actual_state not in TERMINAL_STATES:
        return "pending"
    filled = order.actual_quantity
    if filled is None or order.quantity is None or not ZERO <= filled <= order.quantity:
        return "unknown"
    return "no_fill" if not filled else "filled" if filled == order.quantity else "partial"


def _reason(order, window, arrival, as_of):
    if order.sent_at is None:
        return "missing_send_time"
    if not order.acknowledged:
        return "unacknowledged_actual_order"
    if window is None:
        return "market_not_recorded"
    if order.source is None or order.source == "direct_rest":
        return "direct_quote_unrecorded" if order.source else "quote_source_unrecorded"
    if order.kind == "entry" and order.ready_at is None:
        return "missing_entry_ready_time"
    if order.resting and order.target_until is None:
        return "missing_target_expiry"
    if order.side not in ("UP", "DOWN") or order.limit is None or order.quantity is None:
        return "missing_order_terms"
    if (not order.window_start <= order.sent_at < order.window_end or order.kind == "entry"
            and not order.window_start <= order.ready_at <= order.sent_at):
        return "invalid_order_timing"
    if arrival >= as_of:
        return "arrival_not_observed_yet"
    return None


def _market_row(db, slug, orders, as_of):
    first = orders[0]
    row = db.execute("SELECT slug,start,end,window,payout,settled_at FROM markets WHERE slug=?", (slug,)).fetchone()
    window = decode_window(row[:4]) if row else None
    if window is None or (window.start, window.end) != (first.window_start, first.window_end):
        return None, None
    payout, settled_at = as_decimal(row[4]), number(row[5])
    if (payout is None or not ZERO <= payout <= ONE or settled_at is None or not window.end <= settled_at <= as_of):
        payout = None
    return window, payout


def _target_coverage(row, at, book, halted):
    if row["now_halted"] or row["covered_to"] is None:
        row["covered_to"] = at if row["now_halted"] else row["arrival_at"]
    row["missing_quotes"] |= at - row["covered_to"] > MAX_UNOBSERVED_SECONDS
    row["now_halted"] = halted
    if book is not None:
        row["covered_to"] = min(book.at + BOOK_MAX_AGE, book.exchange_at + EXCHANGE_MAX_AGE)
    elif halted:
        row["covered_to"] = at


def _target_active_until(order, as_of):
    end = min(order.target_until, as_of)
    return min(end, order.cancel_requested_at) if order.cancel_requested_at is not None else end


def _simulate_market(db, orders, as_of, latency, depth_share, fee_coefficient):
    window, payout = _market_row(db, orders[0].slug, orders, as_of)
    timeline = QuoteCursor(window, get_frames(db, window, min(window.end, as_of))) if window is not None else None
    step = as_decimal(window.minimum_quantity) if window is not None else None
    tick = as_decimal(window.price_tick) if window is not None else None
    fee = fee_coefficient if fee_coefficient is not None else as_decimal(window.theta) if window is not None else None
    engine = PaperExecution(depth_share=depth_share)
    probes, status = [], {}
    frame_times = sorted({event[0] for event in timeline.events}) if timeline else []
    for order in orders:
        arrival = order.sent_at + latency if order.sent_at is not None else None
        reason = _reason(order, window, arrival, as_of)
        if reason is None and (step is None or step <= ZERO or tick is None or tick <= ZERO or fee is None or fee < ZERO):
            reason = "missing_market_terms"
        if reason is None and (order.quantity <= ZERO or order.quantity % step or not ZERO < order.limit < ONE
                               or order.limit % tick):
            reason = "invalid_order_terms"
        start = {"outcome": "unverified", "reason": reason, "arrival_at": arrival, "source": None,
                 "filled": ZERO, "gross": ZERO, "fee": ZERO, "valid_target_probe": False,
                 "covered_to": None, "now_halted": False, "missing_quotes": False, "cancel_uncertain": False}
        status[order.order_id] = start
        if reason is not None:
            continue
        if order.resting:
            cutoff = min(order.target_until, as_of)
            if arrival >= cutoff:
                start.update(outcome="expired", reason="arrived_after_target_end")
                continue
            probes.extend((at, order.order_id, order) for at in (arrival, *[t for t in frame_times if arrival < t < cutoff]))
        else:
            probes.append((arrival, order.order_id, order))

    for at, oid, order in sorted(probes, key=lambda item: item[:2]):
        row = status[oid]
        if row["filled"] == order.quantity:
            continue
        entering = order.kind == "entry"
        age = order.entry_exchange_age if entering else EXCHANGE_MAX_AGE
        book, halted, source = timeline.select(at, order.source, age, order.ready_at if entering else None)
        if order.resting:
            observed = book if book is not None and book.at >= row["arrival_at"] else None
            if at >= _target_active_until(order, as_of):
                # The venue may cancel anywhere between the request and its confirmation.
                # An eligible bid here gives no basis to credit or deny a resting fill.
                row["cancel_uncertain"] |= bool(not halted and observed and any(
                    as_decimal(price) >= order.limit and as_decimal(size) * TARGET_DEPTH_SHARE >= step
                    for price, size in observed.side_bids(order.side)
                ))
                continue
            _target_coverage(row, at, observed, halted)
            fill = engine.match_target(book, side=order.side, quantity=order.quantity - row["filled"],
                                       limit=order.limit, arrival_at=at, cutoff=min(order.target_until, as_of),
                                       quantity_step=step, fee_coefficient=fee, market_open=not halted,
                                       activated_at=row["arrival_at"], depth_share=TARGET_DEPTH_SHARE)
            if fill.outcome in MODEL_OUTCOMES:
                row["valid_target_probe"] = True
                row["source"] = source
                if fill.filled_quantity:
                    row["filled"] += fill.filled_quantity
                    row["gross"] += fill.gross
                    row["fee"] += fill.fee
            continue
        fill = engine.match_ioc(book, side=order.side, action="buy" if entering else "sell", quantity=order.quantity,
                                limit=order.limit, arrival_at=at, cutoff=order.window_end, quantity_step=step,
                                fee_coefficient=fee, market_open=not halted, exchange_max_age=age,
                                minimum_quote_at=order.ready_at if entering else None)
        row.update(outcome=fill.outcome, reason=fill.reason, source=source, filled=fill.filled_quantity,
                   gross=fill.gross, fee=fill.fee)
    results = []
    for order in orders:
        row = status[order.order_id]
        if order.resting and row["reason"] is None:
            active_until = _target_active_until(order, as_of)
            if row["filled"] < order.quantity and active_until >= row["arrival_at"]:
                _target_coverage(row, active_until, None, False)
            if row["cancel_uncertain"]:
                row.update(outcome="unverified", reason="target_fill_possible_during_cancel")
            elif row["missing_quotes"]:
                row.update(outcome="unverified", reason="extended_unobserved_target_quotes")
            elif row["valid_target_probe"]:
                state = "filled" if row["filled"] == order.quantity else "partial" if row["filled"] else "no_fill"
                row.update(outcome=state, reason="resting_target_quote_proxy")
            else:
                row.update(outcome="unverified", reason="no_verified_target_book")
        results.append(_order_result(order, row))
    return results, payout


def _order_result(order, row):
    modeled = row["outcome"] in MODEL_OUTCOMES
    quantity = row["filled"] if modeled else None
    price = row["gross"] / quantity if quantity else None
    fee = row["fee"] if modeled else None
    latency = (order.first_fill_at - order.sent_at if order.first_fill_at is not None and order.sent_at is not None
               and order.first_fill_at >= order.sent_at else None)
    return {"order_id": order.order_id, "trade_id": order.trade_id, "slug": order.slug, "kind": order.kind,
            "side": order.side, "limit": order.limit, "requested_quantity": order.quantity, "sent_at": order.sent_at,
            "observed_send_to_first_fill": latency, "actual_state": order.actual_state,
            "actual_outcome": _actual_outcome(order), "actual_quantity": order.actual_quantity,
            "actual_price": order.actual_price, "actual_fee": order.actual_fee,
            "modeled_outcome": row["outcome"], "reason": row["reason"], "modeled_arrival_at": row["arrival_at"],
            "modeled_quote_source": row["source"], "quote_source_inferred": order.source_inferred,
            "resting_target_proxy": order.resting, "cancel_requested_at": order.cancel_requested_at,
            "target_known_end_at": order.target_until, "modeled_quantity": quantity, "modeled_price": price,
            "modeled_fee": fee, "modeled_gross": row["gross"] if modeled else None}


def _metrics(rows):
    matched = [row for row in rows if row["actual_outcome"] in MODEL_OUTCOMES and row["modeled_quantity"] is not None]
    price_pairs = [row for row in matched if row["actual_price"] is not None and row["modeled_price"] is not None]
    fee_pairs = [row for row in matched if row["actual_fee"] is not None and row["modeled_fee"] is not None]
    quantity = sum((row["requested_quantity"] for row in matched), ZERO)
    errors = [(row["modeled_price"] - row["actual_price"]) * (1 if row["kind"] == "entry" else -1) * 100
              for row in price_pairs]
    overlapping_size = [min(row["modeled_quantity"], row["actual_quantity"]) for row in price_pairs]
    overlap = sum(overlapping_size, ZERO)
    return {"orders": len(rows), "actual_outcomes": Counter(row["actual_outcome"] for row in rows),
            "modeled_outcomes": Counter(row["modeled_outcome"] for row in rows), "matched_orders": len(matched),
            "matched_requested_quantity": quantity,
            "actual_matched_filled_quantity": sum((row["actual_quantity"] for row in matched), ZERO),
            "modeled_matched_filled_quantity": sum((row["modeled_quantity"] for row in matched), ZERO),
            "matched_any_fill_counts": {name: sum(row[f"{name}_quantity"] > ZERO for row in matched)
                                        for name in ("actual", "modeled")},
            "paired_outcomes": Counter(f"{row['actual_outcome']}/{row['modeled_outcome']}" for row in matched),
            "price_pairs": len(price_pairs), "mean_modeled_adverse_price_error_cents": sum(errors, ZERO) / len(errors)
            if errors else None,
            "quantity_weighted_adverse_price_error_cents": (
                sum((error * size for error, size in zip(errors, overlapping_size)), ZERO) / overlap if overlap else None
            ),
            "quantity_weighted_absolute_price_error_cents": (
                sum((abs(error) * size for error, size in zip(errors, overlapping_size)), ZERO) / overlap if overlap else None
            ),
            "fee_pairs": len(fee_pairs),
            "actual_paired_fees_usd": sum((row["actual_fee"] for row in fee_pairs), ZERO) if fee_pairs else None,
            "modeled_paired_fees_usd": sum((row["modeled_fee"] for row in fee_pairs), ZERO) if fee_pairs else None,
            "modeled_minus_actual_fees_usd": sum((row["modeled_fee"] - row["actual_fee"] for row in fee_pairs), ZERO)
            if fee_pairs else None,
            "observed_send_to_first_fill_seconds": distribution([row["observed_send_to_first_fill"] for row in rows
                                                                  if row["observed_send_to_first_fill"] is not None]),
            "unverified_reasons": Counter(row["reason"] for row in rows if row["modeled_quantity"] is None)}


def _fixed_entry_pnl(orders, simulated, payout):
    entry = next((order for order in orders if order.kind == "entry"), None)
    if (entry is None or entry.trade_net is None or not entry.trade_fee_exact or not entry.actual_quantity
            or entry.actual_price is None or entry.actual_fee is None or _actual_outcome(entry) not in MODEL_OUTCOMES):
        return None
    if any(order.kind != "entry" and not order.acknowledged and
           (order.actual_state not in ("ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED")) for order in orders):
        return None
    exits = [row for row in simulated if row["kind"] != "entry" and row["actual_outcome"] != "unacknowledged"]
    if any(row["modeled_quantity"] is None or row["actual_outcome"] not in MODEL_OUTCOMES for row in exits):
        return None
    remaining = entry.actual_quantity - sum((row["modeled_quantity"] for row in exits), ZERO)
    if remaining < ZERO or (remaining > ZERO and payout is None):
        return None
    settlement = remaining * (payout if entry.side == "UP" else ONE - payout) if remaining else ZERO
    gross = sum((row["modeled_gross"] - row["modeled_fee"] for row in exits), ZERO)
    return (entry.trade_net, gross + settlement - entry.actual_quantity * entry.actual_price - entry.actual_fee,
            any(row["resting_target_proxy"] or row["quote_source_inferred"] for row in exits))


def report(journal, tape, since, as_of, *, latency=DEFAULT_DELAY, depth_share=ONE, fee_coefficient=None,
           include_orders=False, trade_ids=None):
    orders = load_orders(journal, since, as_of, trade_ids)
    groups = defaultdict(list)
    for order in orders:
        groups[order.slug].append(order)
    results, pnl = [], []
    with readonly(Path(tape)) as db:
        db.execute("BEGIN")
        for group in groups.values():
            modeled, payout = _simulate_market(db, group, as_of, latency, depth_share, fee_coefficient)
            results.extend(modeled)
            paired = _fixed_entry_pnl(group, modeled, payout)
            if paired is not None:
                pnl.append(paired)
    return {"since": since, "as_of": as_of, "orders": len(orders), "latency_seconds": latency,
            "ioc_depth_share": depth_share, "target_depth_share": TARGET_DEPTH_SHARE,
            "order_kinds": {kind: _metrics([row for row in results if row["kind"] == kind])
                            for kind in sorted({row["kind"] for row in results})},
            "fixed_real_entry_pnl": {"matched_closed_trades": len(pnl), "using_quote_proxies": sum(row[2] for row in pnl),
                                      "actual_net": sum((row[0] for row in pnl), ZERO) if pnl else None,
                                      "modeled_exit_net": sum((row[1] for row in pnl), ZERO) if pnl else None},
            **({"order_details": results} if include_orders else {})}


def iso_time(value):
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError
        return instant.timestamp()
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("use an ISO-8601 timestamp with a UTC offset, such as 2026-11-25T07:17:00Z") from exc


def parse_trade_ids(value):
    try:
        numbers = {int(part) for part in value.split(",")}
        if not numbers or min(numbers) < 1:
            raise ValueError
        return numbers
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a comma-separated list of positive live journal trade IDs") from exc


def print_report(result):
    start, end = (datetime.fromtimestamp(result[key], timezone.utc).isoformat() for key in ("since", "as_of"))
    print(f"Oil D exact-order paper replay | {start} to {end} | {result['orders']} journal orders")
    print(f"Modeled IOC arrival +{result['latency_seconds']:g}s | IOC depth {result['ioc_depth_share']:.0%} | "
          f"resting target proxy depth {result['target_depth_share']:.0%}")
    for kind, data in result["order_kinds"].items():
        actual, modeled = data["actual_outcomes"], data["modeled_outcomes"]
        print(f"{kind} | {data['orders']} orders | {data['matched_orders']} with paired evidence")
        print(f"  Actual {actual['filled']} full / {actual['partial']} partial / {actual['no_fill']} zero | "
              f"modeled {modeled['filled']} full / {modeled['partial']} partial / {modeled['no_fill'] + modeled['expired']} zero")
        if data["matched_orders"]:
            print(f"  Paired contracts filled | live {data['actual_matched_filled_quantity']:g} | "
                  f"modeled {data['modeled_matched_filled_quantity']:g} | requested {data['matched_requested_quantity']:g}")
        if data["price_pairs"]:
            print(f"  Mean adverse modeled price error {data['mean_modeled_adverse_price_error_cents']:+.2f}¢ "
                  f"per contract ({data['price_pairs']} both-filled orders)")
            print(f"  Quantity-weighted price error {data['quantity_weighted_adverse_price_error_cents']:+.2f}¢ | "
                  f"absolute error {data['quantity_weighted_absolute_price_error_cents']:.2f}¢")
        if data["fee_pairs"]:
            print(f"  Modeled minus actual fees ${data['modeled_minus_actual_fees_usd']:+.2f} ({data['fee_pairs']} paired orders)")
        if data["unverified_reasons"]:
            print("  Unverified | " + ", ".join(f"{reason} {count}" for reason, count in data["unverified_reasons"].most_common()))
    pnl = result["fixed_real_entry_pnl"]
    if pnl["matched_closed_trades"]:
        print(f"Fixed real entries | {pnl['matched_closed_trades']} complete cases | live net ${pnl['actual_net']:+.2f} | "
              f"modeled exits net ${pnl['modeled_exit_net']:+.2f} | {pnl['using_quote_proxies']} use exit quote proxies")
    print("Resting targets use a quote proxy. Possible fills between cancel request and confirmation are unverified. "
          "Direct REST quotes require recorded depth. Fixed-entry P&L uses actual entry cash flows and official payouts.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=live_path(WTI.directory()))
    parser.add_argument("--tape", type=Path, default=tape_path(WTI.directory()))
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--since", type=iso_time)
    parser.add_argument("--until", type=iso_time)
    parser.add_argument("--trade-ids", type=parse_trade_ids)
    parser.add_argument("--latency", type=float, default=DEFAULT_DELAY, help="modeled send-to-exchange delay in seconds")
    parser.add_argument("--depth-share", type=Decimal, default=ONE, help="share of displayed depth for IOC orders")
    parser.add_argument("--fee-coefficient", type=Decimal, help="optional fee coefficient override")
    parser.add_argument("--json", action="store_true", help="include one row for every selected order")
    args = parser.parse_args()
    end = time.time() if args.until is None else args.until
    start = end - args.hours * 3600 if args.since is None else args.since
    if (not all(map(isfinite, (start, end, args.hours, args.latency))) or args.hours <= 0 or args.latency < 0
            or start >= end or not args.depth_share.is_finite() or not ZERO < args.depth_share <= ONE
            or args.fee_coefficient is not None and (not args.fee_coefficient.is_finite() or args.fee_coefficient < ZERO)):
        parser.error("check the time span, nonnegative latency and fees, and depth share in (0, 1]")
    if not args.journal.is_file() or not args.tape.is_file():
        parser.error("the live journal and WTI replay tape must both exist")
    result = report(args.journal, args.tape, start, end, latency=args.latency, depth_share=args.depth_share,
                    fee_coefficient=args.fee_coefficient, include_orders=args.json, trade_ids=args.trade_ids)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
