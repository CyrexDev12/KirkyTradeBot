"""Durable orders and operator controls for the Oil D live account."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from math import isfinite
from pathlib import Path
import re
import sqlite3


TERMINAL_STATES = frozenset(
    "ORDER_STATE_" + state for state in ("FILLED", "CANCELED", "REPLACED", "REJECTED", "EXPIRED")
)
PILOT_LOSS_PAUSE = "Pilot loss limit reached"
PILOT_LOSS_LIMIT = Decimal("2.00")
_OPERATOR_PAUSE_PREFIX = "New entries stopped by "


def live_path(directory: Path) -> Path:
    return directory / "live" / "live.sqlite3"


def _decimal(value) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite live order amount")
    return format(number, "f")


def _dump(value) -> str:
    def encode(obj):
        if isinstance(obj, Decimal):
            return _decimal(obj)
        raise TypeError(f"Cannot write {type(obj).__name__} to the live journal")

    return json.dumps(value, default=encode, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat() if at is not None else None


def _row(row, *fields):
    if row is None:
        return None
    data = dict(row)
    for name in fields:
        data[name] = json.loads(data[name])
    return data


class LiveStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS control (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    disabled_at REAL, disabled_reason TEXT, heartbeat_at REAL,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                INSERT OR IGNORE INTO control(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS pilot_runs (
                    id INTEGER PRIMARY KEY, pilot_key TEXT NOT NULL UNIQUE, started_at REAL NOT NULL,
                    starting_budget TEXT NOT NULL, baseline_realized_pnl TEXT NOT NULL,
                    last_closed_trade_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY, slug TEXT NOT NULL,
                    window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
                    at REAL NOT NULL, payload TEXT NOT NULL, UNIQUE(slug, at)
                );
                CREATE INDEX IF NOT EXISTS pending_entry_checks ON signals(id)
                    WHERE json_extract(payload,'$.live_entry_check') IS NOT NULL
                    AND json_extract(payload,'$.entry_outcome') IS NULL;
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY, signal_id INTEGER NOT NULL REFERENCES signals(id),
                    slug TEXT NOT NULL UNIQUE, window_start INTEGER NOT NULL UNIQUE,
                    window_end INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, closed_at REAL,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_trade ON trades(state) WHERE state='active';
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL REFERENCES trades(id),
                    kind TEXT NOT NULL, key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    venue_id TEXT UNIQUE, request TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS orders_by_trade ON orders(trade_id, id);
                CREATE TABLE IF NOT EXISTS fills (
                    execution_id TEXT PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                    at REAL NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
                    fee TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, at REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entry_timings (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS entry_timings_at ON entry_timings(at);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _transaction(self, write=False):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    @staticmethod
    def _event(db, kind, at, payload):
        return db.execute("INSERT INTO events(kind,at,payload) VALUES(?,?,?)",
                          (kind, at, _dump(payload))).lastrowid

    def event(self, kind, at, payload):
        with self._transaction(True) as db:
            return self._event(db, kind, at, payload)

    def record_entry_timing(self, at, payload):
        with self._transaction(True) as db:
            db.execute("INSERT INTO entry_timings(at,payload) VALUES(?,?)", (at, _dump(payload)))

    def events(self, after=0, limit=50):
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id,kind,at,payload FROM events WHERE id>? ORDER BY id LIMIT ?",
                (after, limit),
            ).fetchall()
            latest_id = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
        return {"events": [{**json.loads(row["payload"]), "id": row["id"], "kind": row["kind"],
                            "at": _iso(row["at"])} for row in rows], "latest_id": latest_id}

    def enabled(self, configured=True):
        if not configured:
            return False
        with self._transaction() as db:
            return db.execute("SELECT disabled_at FROM control WHERE id=1").fetchone()[0] is None

    def disable(self, at, reason="operator"):
        with self._transaction(True) as db:
            changed = db.execute("""UPDATE control SET disabled_at=?,disabled_reason=?
                                    WHERE id=1 AND disabled_at IS NULL""", (at, reason)).rowcount == 1
            if changed:
                self._event(db, "paused", at, {"title": "Live entries paused", "message": reason})
            return changed

    @staticmethod
    def _active_pilot(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_runs'").fetchone():
            return None
        return _row(db.execute("SELECT * FROM pilot_runs ORDER BY id DESC LIMIT 1").fetchone())

    def active_pilot(self):
        with self._transaction() as db:
            return self._active_pilot(db)

    @staticmethod
    def _start_parameters(pilot_key, starting_budget, at):
        if not isinstance(pilot_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", pilot_key):
            raise ValueError("Use a pilot key with up to 48 lowercase letters, digits, or hyphens.")
        budget = Decimal(_decimal(starting_budget))
        if not 0 < budget <= 12 or not isfinite(at) or at <= 0:
            raise ValueError("Pilot budget must be between $0 and $12 and the start time must be current.")
        return budget

    @staticmethod
    def _start_state(db, pilot_key, budget, at):
        existing = _row(db.execute("SELECT * FROM pilot_runs WHERE pilot_key=?", (pilot_key,)).fetchone())
        current = LiveStore._active_pilot(db)
        if existing is not None:
            if Decimal(existing["starting_budget"]) != budget:
                raise ValueError("That pilot key already has a different starting budget.")
            if current != existing:
                raise ValueError("That pilot key belongs to a previous run.")
            return existing

        control = db.execute("SELECT * FROM control WHERE id=1").fetchone()
        reason = control["disabled_reason"] or ""
        if control["disabled_at"] is None or not (reason.startswith(_OPERATOR_PAUSE_PREFIX) or reason == PILOT_LOSS_PAUSE):
            raise ValueError("Pause Oil D and resolve any reconciliation issue before starting a pilot.")
        data = json.loads(control["payload"])
        heartbeat = control["heartbeat_at"]
        if (heartbeat is None or not 0 <= at - heartbeat < 30 or not data.get("worker_configured")
                or not data.get("entries_configured")):
            raise ValueError("A current, configured Oil D worker is required before starting a pilot.")
        if current is not None and at <= current["started_at"]:
            raise ValueError("The next pilot must start after the current pilot.")
        terminal = tuple(sorted(TERMINAL_STATES))
        placeholders = ",".join("?" for _ in terminal)
        if (db.execute("SELECT 1 FROM trades WHERE state='active' LIMIT 1").fetchone()
                or db.execute(f"SELECT 1 FROM orders WHERE state NOT IN ({placeholders}) LIMIT 1", terminal).fetchone()):
            raise ValueError("Oil D still has an active position or an unresolved order in the journal.")
        return None

    def pilot_start_ready(self, pilot_key, starting_budget, at):
        budget = self._start_parameters(pilot_key, starting_budget, at)
        with self._transaction() as db:
            return self._start_state(db, pilot_key, budget, at)

    def start_pilot(self, pilot_key, starting_budget, at, buying_power):
        """Freeze a verified new risk baseline in the paused, flat journal."""
        budget = self._start_parameters(pilot_key, starting_budget, at)
        with self._transaction(True) as db:
            existing = self._start_state(db, pilot_key, budget, at)
            if existing is not None:
                return existing, False
            if Decimal(_decimal(buying_power)) < budget:
                raise ValueError("The exchange buying power is below the requested pilot budget.")
            realized = self._realized(db)
            cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM trades WHERE closed_at IS NOT NULL").fetchone()[0]
            db.execute("""INSERT INTO pilot_runs(pilot_key,started_at,starting_budget,baseline_realized_pnl,last_closed_trade_id)
                VALUES(?,?,?,?,?)""", (pilot_key, at, _decimal(budget), _decimal(realized), cursor))
            db.execute("""UPDATE control SET disabled_at=?,disabled_reason=? WHERE id=1 AND disabled_reason=?""",
                       (at, f"{_OPERATOR_PAUSE_PREFIX}the VM operator for pilot {pilot_key}.", PILOT_LOSS_PAUSE))
            current = self._active_pilot(db)
            self._event(db, "pilot_started", at, {"title": "New Oil D pilot configured", "pilot_run": current,
                                                 "message": f"Pilot {pilot_key} is allocated ${budget:.2f} and remains paused."})
            return current, True

    def resume_operator_pause(self, at):
        with self._transaction(True) as db:
            control = db.execute("SELECT disabled_reason,heartbeat_at,payload FROM control WHERE id=1").fetchone()
            data = json.loads(control["payload"])
            if (not (control["disabled_reason"] or "").startswith(_OPERATOR_PAUSE_PREFIX)
                    or control["heartbeat_at"] is None or not 0 <= at - control["heartbeat_at"] < 30
                    or not data.get("worker_configured") or not data.get("entries_configured")
                    or db.execute("SELECT 1 FROM trades WHERE state='active'").fetchone()):
                return False
            pilot = self._active_pilot(db)
            if pilot is not None:
                try:
                    effective = Decimal(_decimal(data.get("pilot_budget"))) if data.get("pilot_budget") is not None else 0
                except (ValueError, ArithmeticError):
                    return False
                if (control["heartbeat_at"] < pilot["started_at"] or data.get("pilot_run_key") != pilot["pilot_key"]
                        or not 0 < effective <= Decimal(pilot["starting_budget"])
                        or self._realized(db) - Decimal(pilot["baseline_realized_pnl"]) <= -PILOT_LOSS_LIMIT):
                    return False
            db.execute("UPDATE control SET disabled_at=NULL,disabled_reason=NULL WHERE id=1")
            self._event(db, "resumed", at, {"title": "Live entries resumed",
                                           "message": "The VM operator resumed the Oil D pilot."})
            return True

    def heartbeat(self, at, status_payload):
        with self._transaction(True) as db:
            db.execute("""UPDATE control SET heartbeat_at=?,payload=?
                          WHERE id=1 AND (heartbeat_at IS NULL OR heartbeat_at<=?)""",
                       (at, _dump(status_payload), at))

    def status(self, configured=True):
        with self._transaction() as db:
            control = db.execute("SELECT * FROM control WHERE id=1").fetchone()
            active = _row(db.execute("SELECT * FROM trades WHERE state='active'").fetchone(), "payload")
            latest_id = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
            pilot = self._active_pilot(db)
            last_entry = db.execute("""SELECT payload,at FROM events WHERE kind IN ('entry_skipped','entry_unfilled')
                                     ORDER BY id DESC LIMIT 1""").fetchone()
        payload = json.loads(control["payload"])
        if pilot is not None and payload.get("pilot_run_key") != pilot["pilot_key"]:
            payload.update(pilot_budget=pilot["starting_budget"], pilot_net_pnl="0", pilot_equity=pilot["starting_budget"])
        paused = control["disabled_at"] is not None
        if paused:
            state, message = "paused", "New live entries are paused."
        elif not configured:
            state, message = "disabled", "Live trading is not configured."
        else:
            state = payload.get("state", "starting")
            message = payload.get("message", "Waiting for the live worker.")
        return {**payload, "pilot_run": pilot, "configured": bool(configured), "enabled": bool(configured and not paused),
                "paused": paused, "disabled_at": _iso(control["disabled_at"]),
                "disabled_reason": control["disabled_reason"], "last_heartbeat": _iso(control["heartbeat_at"]),
                "latest_id": latest_id, "state": state, "message": message, "active_trade": active,
                "last_missed_entry": {**json.loads(last_entry[0]), "at": _iso(last_entry[1])} if last_entry else None,
                "worker_state": payload.get("state"), "worker_message": payload.get("message")}

    def record_signal(self, slug, start, end, at, payload):
        with self._transaction(True) as db:
            db.execute("""INSERT INTO signals(slug,window_start,window_end,at,payload) VALUES(?,?,?,?,?)
                          ON CONFLICT(slug,at) DO NOTHING""", (slug, start, end, at, _dump(payload)))
            return db.execute("SELECT id FROM signals WHERE slug=? AND at=?", (slug, at)).fetchone()[0]

    def signal(self, signal_id):
        with self._transaction() as db:
            return _row(db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone(), "payload")

    def check_signal(self, signal_id, message):
        with self._transaction(True) as db:
            db.execute("""UPDATE signals SET payload=json_set(payload,'$.live_entry_check',?) WHERE id=?
                          AND json_extract(payload,'$.entry_outcome') IS NULL""", (message, signal_id))

    def publish_expired_signals(self, now, ttl, exit_buffer):
        with self._transaction(True) as db:
            rows = db.execute("""SELECT s.id,s.slug,s.at,s.payload FROM signals s
                WHERE json_extract(s.payload,'$.live_entry_check') IS NOT NULL
                AND json_extract(s.payload,'$.entry_outcome') IS NULL
                AND (json_extract(s.payload,'$.ready_at')+?<?
                     OR min(json_extract(s.payload,'$.deadline'),s.window_end-?)<=?+2)
                AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.signal_id=s.id)
                ORDER BY s.id""", (ttl, now, exit_buffer, now)).fetchall()
            for row in rows:
                data = json.loads(row["payload"])
                self._event(db, "entry_skipped", now, {"title": "Entry skipped", "slug": row["slug"],
                            "side": data.get("side"), "signal_at": _iso(row["at"]),
                            "message": data["live_entry_check"]})
                db.execute("UPDATE signals SET payload=json_set(payload,'$.entry_outcome','skipped') WHERE id=?", (row["id"],))

    @staticmethod
    def _add_order(db, trade, kind, idempotency_key, at, request):
        request = {**request, "quantity": _decimal(request["quantity"]), "price": _decimal(request["price"])}
        encoded = _dump(request)
        existing = _row(db.execute("SELECT * FROM orders WHERE key=?", (idempotency_key,)).fetchone(),
                        "request", "payload")
        if existing:
            if (existing["trade_id"], existing["kind"], _dump(existing["request"])) != (trade["id"], kind, encoded):
                raise ValueError("A live order ID was reused for a different request")
            return existing
        cursor = db.execute("""INSERT INTO orders(trade_id,kind,key,state,created_at,updated_at,request)
                               VALUES(?,?,?,'prepared',?,?,?)""",
                            (trade["id"], kind, idempotency_key, at, at, encoded))
        order = _row(db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone(),
                     "request", "payload")
        LiveStore._event(db, "order_prepared", at, LiveStore._order_event(trade["slug"], order))
        return order

    @staticmethod
    def _order_event(slug, order):
        request = order["request"]
        result = order["payload"]
        return {"slug": slug, "phase": order["kind"], "side": request.get("side"),
                "quantity": result.get("filled") or request["quantity"],
                "price": result.get("average_price") or request["price"], "state": order["state"],
                "message": result.get("reason"), "order": order}

    @staticmethod
    def _write_order(db, order, state, at, venue_id, payload):
        previous = order["payload"]
        if (state, venue_id, payload) == (order["state"], order["venue_id"], previous):
            return order
        transition = (state, venue_id) != (order["state"], order["venue_id"])
        for name in ("filled", "remaining", "average_price", "fees"):
            before, after = previous.get(name), payload.get(name)
            if before is not None and after is not None:
                before, after = Decimal(before), Decimal(after)
            transition |= before != after
        transition |= previous.get("reason") != payload.get("reason")
        db.execute("UPDATE orders SET state=?,updated_at=?,venue_id=?,payload=? WHERE id=?",
                   (state, at, venue_id, _dump(payload), order["id"]))
        order.update(state=state, updated_at=at, venue_id=venue_id, payload=payload)
        if transition:
            slug = db.execute("SELECT slug FROM trades WHERE id=?", (order["trade_id"],)).fetchone()[0]
            LiveStore._event(db, "order_update", at, LiveStore._order_event(slug, order))
        return order

    def claim_entry(self, signal_id, idempotency_key, at, request):
        """Save the trade and its signed-request identity before sending anything."""
        with self._transaction(True) as db:
            if db.execute("SELECT disabled_at FROM control WHERE id=1").fetchone()[0] is not None:
                return None
            signal = db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            if signal is None or at >= signal["window_end"]:
                return None
            pilot = self._active_pilot(db)
            if pilot is not None and signal["at"] < pilot["started_at"]:
                return None
            if db.execute("SELECT 1 FROM trades WHERE state='active' OR window_start=?", (signal["window_start"],)).fetchone():
                return None
            cursor = db.execute("""INSERT INTO trades(signal_id,slug,window_start,window_end,state,created_at,updated_at)
                                   VALUES(?,?,?,?,'active',?,?)""",
                                (signal_id, signal["slug"], signal["window_start"], signal["window_end"], at, at))
            db.execute("UPDATE signals SET payload=json_set(payload,'$.entry_outcome','claimed') WHERE id=?", (signal_id,))
            trade = db.execute("SELECT * FROM trades WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._add_order(db, trade, "entry", idempotency_key, at, request)

    def authorize_entry(self, order_id, at):
        """Commit the handoff to the sender under the same lock as an operator pause."""
        with self._transaction(True) as db:
            order = _row(db.execute("""SELECT * FROM orders WHERE id=? AND kind='entry'
                                       AND state='prepared' AND venue_id IS NULL""", (order_id,)).fetchone(),
                         "request", "payload")
            if order is None or at < order["updated_at"]:
                return False
            trade = db.execute("""SELECT t.window_end,s.payload FROM trades t JOIN signals s ON s.id=t.signal_id
                                   WHERE t.id=? AND t.state='active'""", (order["trade_id"],)).fetchone()
            if trade is None:
                return False
            paused = db.execute("SELECT disabled_at FROM control WHERE id=1").fetchone()[0] is not None
            deadline = min(trade["window_end"], float(json.loads(trade["payload"]).get("deadline", trade["window_end"])))
            if paused or at >= deadline:
                reason = "Live entries were paused before submission." if paused else "The entry deadline passed."
                payload = {**order["payload"], "filled": "0", "reason": reason}
                self._write_order(db, order, "ORDER_STATE_CANCELED", at, None, payload)
                return False
            self._write_order(db, order, "sending", at, None, order["payload"])
            return True

    def prepare_order(self, trade_id, kind, idempotency_key, at, request):
        if kind.lower() == "entry":
            raise ValueError("Live entries must claim a window under the pause lock")
        with self._transaction(True) as db:
            trade = db.execute("SELECT * FROM trades WHERE id=? AND state='active'", (trade_id,)).fetchone()
            if trade is None:
                raise ValueError("Live exit requires an active trade")
            return self._add_order(db, trade, kind, idempotency_key, at, request)

    def update_order(self, order_id, state, at, venue_id=None, payload=None):
        with self._transaction(True) as db:
            order = _row(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone(),
                         "request", "payload")
            if order is None:
                raise ValueError("Unknown live order")
            if order["venue_id"] and venue_id and order["venue_id"] != venue_id:
                raise ValueError("Live order cannot change its exchange ID")
            if at < order["updated_at"]:
                return order
            supplied = json.loads(_dump(payload or {}))
            for name in ("filled", "remaining", "average_price", "fees"):
                if supplied.get(name) is not None:
                    supplied[name] = _decimal(supplied[name])
                else:
                    supplied.pop(name, None)
            previous = order["payload"]
            old_filled = Decimal(previous.get("filled", "0"))
            filled = Decimal(supplied["filled"]) if "filled" in supplied else old_filled
            if filled < old_filled:
                return order
            updated = {**previous, **supplied}
            if filled > old_filled:
                for name in ("average_price", "fees"):
                    if name not in supplied:
                        updated.pop(name, None)
            return self._write_order(db, order, state, at, venue_id or order["venue_id"], updated)

    def record_fill(self, order_id, execution_id, at, quantity, price, fee, payload=None):
        with self._transaction(True) as db:
            cursor = db.execute("""INSERT INTO fills(execution_id,order_id,at,quantity,price,fee,payload)
                                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(execution_id) DO NOTHING""",
                                (execution_id, order_id, at, _decimal(quantity), _decimal(price), _decimal(fee),
                                 _dump(payload or {})))
            if cursor.rowcount == 0:
                return False
            row = db.execute("""SELECT t.slug,o.kind FROM orders o JOIN trades t ON o.trade_id=t.id
                                WHERE o.id=?""", (order_id,)).fetchone()
            self._event(db, "fill", at, {"slug": row["slug"], "phase": row["kind"], "quantity": _decimal(quantity),
                                         "price": _decimal(price), "fee": _decimal(fee),
                                         "execution_id": execution_id, "order_id": order_id})
            return True

    def active_trade(self):
        with self._transaction() as db:
            return _row(db.execute("SELECT * FROM trades WHERE state='active'").fetchone(), "payload")

    def trades(self, limit=None):
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (-1 if limit is None else limit,))
            return [_row(row, "payload") for row in rows]

    def realized(self):
        with self._transaction() as db:
            return self._realized(db)

    @staticmethod
    def _realized(db):
        rows = db.execute("SELECT payload FROM trades WHERE closed_at IS NOT NULL")
        return sum((Decimal(str(value)) for row in rows if (value := json.loads(row[0]).get("net_pnl")) is not None),
                   Decimal(0))

    def orders(self, trade_id):
        with self._transaction() as db:
            return [_row(row, "request", "payload") for row in
                    db.execute("SELECT * FROM orders WHERE trade_id=? ORDER BY id", (trade_id,))]

    def fills(self, trade_id):
        with self._transaction() as db:
            return [_row(row, "payload") for row in db.execute("""
                SELECT f.* FROM fills f JOIN orders o ON f.order_id=o.id WHERE o.trade_id=? ORDER BY f.at,f.execution_id
            """, (trade_id,))]

    def set_trade(self, trade_id, at, payload):
        with self._transaction(True) as db:
            row = db.execute("SELECT payload FROM trades WHERE id=? AND state='active'", (trade_id,)).fetchone()
            if row is None:
                return False
            db.execute("UPDATE trades SET payload=?,updated_at=? WHERE id=?",
                       (_dump({**json.loads(row["payload"]), **payload}), at, trade_id))
            return True

    def close_trade(self, trade_id, at, state, payload=None):
        if state == "active":
            raise ValueError("Closing a trade requires a terminal state")
        with self._transaction(True) as db:
            row = db.execute("SELECT * FROM trades WHERE id=? AND state='active'", (trade_id,)).fetchone()
            if row is None:
                return False
            result = {**json.loads(row["payload"]), **(payload or {})}
            db.execute("UPDATE trades SET state=?,updated_at=?,closed_at=?,payload=? WHERE id=?",
                       (state, at, at, _dump(result), trade_id))
            kind = "entry_unfilled" if state == "NO_FILL" else "trade_closed"
            self._event(db, kind, at, {**result, "phase": state, "slug": row["slug"], "trade_id": trade_id})
            return True
