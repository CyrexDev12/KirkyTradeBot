"""Isolated SQLite journal for the BTC account and its Discord events."""
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

from .market import PRODUCT
from .performance import pattern_label, performance, recent_patterns, record, summarize_pattern
from .strategy import PHOENIX, paper_entry, paper_exit

STARTING_BALANCE = 100.0


def data_directory():
    default = Path(__file__).resolve().parents[2] / "data"
    return Path(os.getenv("NFL_DATA_DIR", default)) / "crypto"


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS decisions(
                    slot TEXT PRIMARY KEY, status TEXT NOT NULL, net_pnl REAL NOT NULL DEFAULT 0,
                    local_day TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_open ON decisions(status) WHERE status='OPEN';
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, slot TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(slot,kind));
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self, write=False):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    @staticmethod
    def _open(db):
        row = db.execute("SELECT payload FROM decisions WHERE status='OPEN'").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _balance(db):
        return round(STARTING_BALANCE + db.execute("SELECT coalesce(sum(net_pnl),0) FROM decisions").fetchone()[0], 8)

    def open_trade(self):
        with self.transaction() as db:
            return self._open(db)

    def has_slot(self, slot):
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM decisions WHERE slot=?", (slot.isoformat(),)).fetchone() is not None

    @staticmethod
    def _event(db, kind, trade, now):
        db.execute("INSERT INTO events(slot,kind,at,payload) VALUES(?,?,?,?)",
                   (trade["slot_at"], kind, now.isoformat(), json.dumps(trade, allow_nan=False)))

    def decide(self, slot, now, reason, signal=None, quote=None):
        """Record at most one decision and one event per slot, even with competing workers."""
        key = slot.isoformat()
        local_day = slot.astimezone(PHOENIX).date().isoformat()
        with self.transaction(True) as db:
            if db.execute("SELECT 1 FROM decisions WHERE slot=?", (key,)).fetchone():
                return
            balance = self._balance(db)
            today = db.execute("SELECT coalesce(sum(net_pnl),0) FROM decisions WHERE local_day=?", (local_day,)).fetchone()[0]
            entry = None
            learning = None
            if self._open(db):
                reason = "previous position still open"
            elif today <= -0.015 * (balance - today):
                reason = "daily realized loss limit"
            elif signal and signal.direction and quote and not reason:
                label = pattern_label(slot, signal.direction)
                patterns = recent_patterns(db, now)
                learning = patterns.get(label) or summarize_pattern(label)
                entry, reason = paper_entry(signal, quote, balance, learning["risk_multiplier"])
            trade = {"slot_at": key, "product": PRODUCT, "decided_at": now.isoformat(), "status": "SKIPPED",
                     "direction": None, "reason": reason or (signal.reason if signal else "no signal")}
            if signal:
                trade.update(confidence=signal.confidence, volatility=signal.volatility, indicators=signal.indicators)
            if learning:
                trade["learning"] = learning
            if entry:
                trade.update(entry, status="OPEN", opened_at=now.isoformat(), entry_quote_at=quote.at.isoformat(),
                             expires_at=(slot + timedelta(minutes=15)).isoformat())
            db.execute("INSERT INTO decisions(slot,status,local_day,payload) VALUES(?,?,?,?)",
                       (key, trade["status"], local_day, json.dumps(trade, allow_nan=False)))
            self._event(db, "opened" if entry else "skipped", trade, now)

    def observe(self, slot_key, quote, now):
        with self.transaction(True) as db:
            trade = self._open(db)
            if not trade or trade["slot_at"] != slot_key or quote.at < datetime.fromisoformat(trade["entry_quote_at"]):
                return
            exit_data = paper_exit(trade, quote, now)
            if exit_data:
                trade.update(exit_data)
                db.execute("UPDATE decisions SET status='CLOSED',net_pnl=?,payload=? WHERE slot=?",
                           (trade["net_pnl"], json.dumps(trade, allow_nan=False), slot_key))
                self._event(db, "closed", {**trade, "record": record(db)}, now)

    def stats(self, now=None):
        with self.transaction() as db:
            return performance(db, now or datetime.now(timezone.utc))

    def account(self):
        with self.transaction() as db:
            balance = self._balance(db)
            position = self._open(db)
            latest = db.execute("SELECT payload FROM decisions ORDER BY slot DESC LIMIT 1").fetchone()
            reserved = position["entry_price"] * position["quantity"] + position["entry_fee"] if position else 0
            return {"mode": "PAPER_ONLY", "product": PRODUCT, "starting_balance": STARTING_BALANCE,
                    "balance": balance, "available_cash": round(balance - reserved, 8),
                    "realized_pnl": round(balance - STARTING_BALANCE, 8), "open_trade": position,
                    "closed_trades": db.execute("SELECT count(*) FROM decisions WHERE status='CLOSED'").fetchone()[0],
                    "latest_decision": json.loads(latest[0]) if latest else None,
                    "latest_id": db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]}

    def trades(self, limit):
        with self.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM decisions ORDER BY slot DESC LIMIT ?", (limit,))]

    def events(self, after, limit):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit))
            return {"events": [{"id": r["id"], "kind": r["kind"], "at": r["at"], "trade": json.loads(r["payload"])}
                               for r in rows], "latest_id": db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]}
