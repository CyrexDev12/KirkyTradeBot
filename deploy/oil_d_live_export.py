"""Read Oil D live and WebSocket shadow journals for export without changing them."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3


LIVE_FIELDS = (
    "trade_id", "order_id", "venue_order_id", "market_slug", "window_start", "window_end", "side", "signal_at",
    "signal_source", "signal_book_received_at", "signal_book_exchange_at", "quote_source", "book_mode",
    "order_created_at", "order_submitted_at", "order_updated_at",
    "first_fill_at", "last_fill_at", "first_fill_exchange_at", "last_fill_exchange_at", "recorded_fill_count",
    "requested_quantity", "limit_price", "filled_quantity",
    "fill_price", "trade_state", "order_state", "outcome", "reason", "closed_at", "entry_fees_usd", "total_fees_usd",
    "fees_exact", "net_pnl_usd", "exit_reason",
)
ATTEMPT_FIELDS = (
    "market_slug", "signal_at", "attempt_at", "signal_source", "signal_book_received_at", "signal_book_exchange_at",
    "quote_source", "book_mode", "side", "outcome",
    "reason", "message", "limit_price", "requested_quantity", "filled_quantity", "acknowledged", "order_state",
    "ready_at", "deadline", "attempt_started_at", "checks_completed_at", "book_search_started_at",
    "book_search_finished_at", "quote_selected_at", "decision_at", "order_submitted_at", "acknowledged_at",
    "best_ask", "eligible_depth", "required_depth", "max_exchange_age_seconds",
)
SHADOW_FIELDS = (
    "market_slug", "signal_at", "signal_source", "signal_book_received_at", "signal_book_exchange_at",
    "quote_source", "side", "signal_recorded_at", "execution_at",
    "ready_at", "deadline", "requested_quantity", "limit_price", "filled_quantity", "fill_price", "first_fill_at",
    "outcome", "reason", "best_ask", "net_edge", "signal_best_ask", "signal_net_edge", "fair_up", "min_edge",
    "eligible_depth", "required_depth", "entry_fees_usd", "entry_cost_usd", "net_pnl_usd", "fees_exact",
    "original_signal_at", "original_side", "original_limit", "original_fair_up", "original_best_ask",
    "original_net_edge", "ask_delta_cents",
)


def _read_database(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return closing(db)


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _cutoff(as_of):
    return float("inf") if as_of is None else as_of


def _project(fields, values):
    return {field: values.get(field) for field in fields}


def _attempt_rows(db, cutoff):
    if not _has_table(db, "entry_timings"):
        return []
    rows = []
    for row in db.execute("SELECT at,payload FROM entry_timings WHERE at<=? ORDER BY at DESC,id DESC", (cutoff,)):
        attempt = json.loads(row["payload"])
        rows.append(_project(ATTEMPT_FIELDS, {
            **attempt, "market_slug": attempt.get("slug"), "attempt_at": row["at"],
            "signal_source": attempt.get("signal_source") or "paper", "quote_source": attempt.get("final_quote_source"),
            "limit_price": attempt.get("limit"), "requested_quantity": attempt.get("quantity"),
            "filled_quantity": attempt.get("filled"), "order_submitted_at": attempt.get("submit_started_at"),
        }))
    return rows


def live_attempt_rows(db_path, *, as_of=None):
    """Return all retained live checks, including signals skipped before an order was prepared."""
    path = Path(db_path)
    if not path.is_file():
        return []
    with _read_database(path) as db:
        return _attempt_rows(db, _cutoff(as_of))


def _linked_attempts(rows):
    linked = {}
    for attempt in rows:
        key = attempt["market_slug"], attempt["signal_at"]
        if key not in linked or (attempt["order_submitted_at"] is not None
                                 and linked[key]["order_submitted_at"] is None):
            linked[key] = attempt
    return linked


def _recorded_fills(db, cutoff):
    if not _has_table(db, "fills"):
        return {}
    return {row["order_id"]: dict(row) for row in db.execute("""
        SELECT f.order_id,MIN(f.at) AS first_fill_at,MAX(f.at) AS last_fill_at,COUNT(*) AS recorded_fill_count,
               MIN(json_extract(f.payload,'$.exchange_at')) AS first_fill_exchange_at,
               MAX(json_extract(f.payload,'$.exchange_at')) AS last_fill_exchange_at
        FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.kind='entry' AND f.at<=? GROUP BY f.order_id
    """, (cutoff,))}


def live_rows(db_path, *, as_of=None):
    """Return current durable entry orders and realized results for the same trade.

    A cutoff omits order versions updated after it and excludes later trade results.
    Unknown fill quantities and missing execution receipt times remain blank.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    cutoff = _cutoff(as_of)
    with _read_database(path) as db:
        db.execute("BEGIN")
        attempts = _linked_attempts(_attempt_rows(db, cutoff))
        fills = _recorded_fills(db, cutoff)
        orders = db.execute("""
            SELECT t.id AS trade_id,o.id AS order_id,o.venue_id AS venue_order_id,t.slug AS market_slug,
                   t.window_start,t.window_end,s.at AS signal_at,t.state AS trade_state,o.state AS order_state,
                   o.created_at AS order_created_at,o.updated_at AS order_updated_at,t.closed_at,
                   s.payload AS signal_payload,o.request,o.payload AS order_payload,t.payload AS trade_payload
            FROM orders o JOIN trades t ON t.id=o.trade_id JOIN signals s ON s.id=t.signal_id
            WHERE o.kind='entry' AND o.created_at<=? AND o.updated_at<=? ORDER BY o.created_at DESC,o.id DESC
        """, (cutoff, cutoff))
        return [_flatten_live(order, attempts, fills, cutoff) for order in orders]


def _flatten_live(order, attempts, fills, cutoff):
    request = json.loads(order["request"])
    signal = json.loads(order["signal_payload"])
    entry = json.loads(order["order_payload"])
    attempt = attempts.get((order["market_slug"], order["signal_at"]), {})
    closed_at = order["closed_at"]
    closed = closed_at is not None and closed_at <= cutoff
    trade = json.loads(order["trade_payload"]) if closed else {}
    return _project(LIVE_FIELDS, {
        **dict(order), **fills.get(order["order_id"], {}),
        "side": request.get("side") or signal.get("side"),
        "signal_source": request.get("signal_source") or signal.get("signal_source") or attempt.get("signal_source") or "paper",
        "signal_book_received_at": signal.get("signal_book_received_at", attempt.get("signal_book_received_at")),
        "signal_book_exchange_at": signal.get("signal_book_exchange_at", attempt.get("signal_book_exchange_at")),
        "quote_source": attempt.get("quote_source") or request.get("quote_source"), "book_mode": attempt.get("book_mode"),
        "order_submitted_at": attempt.get("order_submitted_at"), "requested_quantity": request.get("quantity"),
        "limit_price": request.get("price"), "filled_quantity": entry.get("filled"), "fill_price": entry.get("average_price"),
        "trade_state": order["trade_state"] if closed_at is None or closed else "active",
        "closed_at": closed_at if closed else None, "outcome": attempt.get("outcome"),
        "reason": attempt.get("reason") or entry.get("reason") or trade.get("message"), "entry_fees_usd": entry.get("fees"),
        "total_fees_usd": trade.get("fees"), "fees_exact": trade.get("fees_exact"), "net_pnl_usd": trade.get("net_pnl"),
        "exit_reason": trade.get("exit_reason"),
    })


def shadow_rows(db_path, *, as_of=None):
    """Return shadow signals and execution evidence matched by contract and signal time."""
    path = Path(db_path)
    if not path.is_file():
        return []
    stage_rows = {}
    with _read_database(path) as db:
        rows = db.execute("""
            SELECT slug,signal_at,stage,at,payload FROM oil_d_shadow
            WHERE stage IN ('signal','execution') AND at<=? ORDER BY signal_at DESC,slug,stage
        """, (_cutoff(as_of),))
        for slug, signal_at, stage, at, payload in rows:
            stage_rows.setdefault((slug, signal_at), {})[stage] = (at, json.loads(payload))
    return [_flatten_shadow(slug, signal_at, stages) for (slug, signal_at), stages in stage_rows.items()]


def _flatten_shadow(slug, signal_at, stages):
    signal_at_recorded, signal = stages.get("signal", (None, {}))
    execution_at, execution = stages.get("execution", (None, {}))
    evidence = {**signal, **execution}
    fill = execution.get("fill") or {}
    return _project(SHADOW_FIELDS, {
        **evidence, "market_slug": slug, "signal_at": signal_at,
        "signal_source": signal.get("source") or evidence.get("source") or "stream",
        "signal_book_received_at": signal.get("book_received_at"), "signal_book_exchange_at": signal.get("book_exchange_at"),
        "quote_source": execution.get("source"), "signal_recorded_at": signal_at_recorded, "execution_at": execution_at,
        "requested_quantity": signal.get("requested_quantity", signal.get("quantity")),
        "limit_price": fill.get("limit", evidence.get("shadow_limit")),
        "filled_quantity": fill.get("quantity"), "fill_price": fill.get("average_price"),
        "first_fill_at": fill.get("filled_at"), "entry_fees_usd": fill.get("fee"), "entry_cost_usd": fill.get("cost"),
        "signal_best_ask": signal.get("best_ask"), "signal_net_edge": signal.get("net_edge"),
        "net_pnl_usd": None, "fees_exact": None,
    })
