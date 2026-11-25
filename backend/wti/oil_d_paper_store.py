"""Independent, durable ledger for the Oil D execution paper account."""

from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from math import isfinite
from pathlib import Path
import sqlite3

from .live_timing import readonly


STARTING_BUDGET = Decimal("100.00")
PAPER_ENTRY_CAP = (STARTING_BUDGET * Decimal(".10")).quantize(Decimal(".01"))
PAPER_LOSS_CAP = (STARTING_BUDGET * Decimal(".20")).quantize(Decimal(".01"))
ZERO = Decimal(0)
ACTIVE = ("sent", "open", "settling")
TRANSITIONS = {"sent": {"open", "no_fill", "unverified"}, "open": {"open", "settling", "closed"},
               "settling": {"settling", "closed"}}


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def paper_path(directory: Path) -> Path:
    return directory / "paper-execution" / "paper.sqlite3"


def _encode(value):
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError(f"Unsupported paper ledger value {type(value).__name__}")


def _dump(payload):
    return json.dumps(payload, default=_encode, allow_nan=False, separators=(",", ":"))


def _trade(row):
    if row is None:
        return None
    trade = dict(row)
    trade["payload"] = json.loads(trade["payload"])
    return trade


class PaperAccount:
    def __init__(self, path, now, *, live_journal=None):
        self.path = Path(path)
        self.live_journal = Path(live_journal) if live_journal is not None else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL NOT NULL,
                    realized TEXT NOT NULL DEFAULT '0'
                );
                CREATE TABLE IF NOT EXISTS trades (
                    slug TEXT PRIMARY KEY, window_start INTEGER NOT NULL UNIQUE, window_end INTEGER NOT NULL,
                    signal_at REAL NOT NULL, sent_at REAL NOT NULL, updated_at REAL NOT NULL,
                    closed_at REAL, state TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS paper_one_active ON trades((1))
                    WHERE state IN ('sent','open','settling');
                CREATE INDEX IF NOT EXISTS paper_trades_sent ON trades(sent_at);
                CREATE TABLE IF NOT EXISTS entry_skips (
                    slug TEXT NOT NULL, signal_at REAL NOT NULL, at REAL NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(slug, signal_at)
                );
                CREATE INDEX IF NOT EXISTS paper_skips_at ON entry_skips(at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_status (
                    id INTEGER PRIMARY KEY CHECK(id=1), heartbeat_at REAL NOT NULL,
                    entries_allowed INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT
                );
            """)
            db.execute("INSERT OR IGNORE INTO account(id,started_at) VALUES(1,?)", (now,))

    def _connect(self, timeout=2):
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _write(self, timeout=2):
        with closing(self._connect(timeout)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @contextmanager
    def _live_gate(self, *, lock=False):
        """Read durable live control, optionally serializing against pause/resume until commit."""
        db, paused_at, reason = None, None, "live_control_unavailable"
        try:
            if self.live_journal is not None:
                access = "rw" if lock else "ro"
                db = sqlite3.connect(self.live_journal.resolve().as_uri() + f"?mode={access}", uri=True, timeout=.15)
                if lock:
                    db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT disabled_at FROM control WHERE id=1").fetchone()
                if row is not None:
                    if row[0] is None:
                        reason = "live_entries_enabled"
                    elif isinstance(row[0], (int, float)) and isfinite(row[0]):
                        paused_at, reason = row[0], None
        except (OSError, sqlite3.Error):
            pass
        try:
            yield paused_at, reason
        finally:
            if db is not None:
                db.close()

    def entry_gate(self):
        with self._live_gate() as (paused_at, reason):
            return paused_at is not None, reason

    def active(self):
        with closing(self._connect()) as db:
            return _trade(db.execute("SELECT * FROM trades WHERE state IN ('sent','open','settling')").fetchone())

    def realized(self):
        with closing(self._connect()) as db:
            return self._realized(db)

    @staticmethod
    def _realized(db):
        return Decimal(db.execute("SELECT realized FROM account WHERE id=1").fetchone()[0])

    def _event(self, db, kind, at, payload):
        realized = self._realized(db)
        snapshot = {**payload, "realized_pnl": realized, "balance": STARTING_BUDGET + realized}
        db.execute("INSERT INTO events(at,kind,payload) VALUES(?,?,?)", (at, kind, _dump(snapshot)))

    def heartbeat(self, now, allowed, reason, *, error=False):
        with self._write() as db:
            previous = db.execute("SELECT * FROM worker_status WHERE id=1").fetchone()
            if previous is not None and previous["heartbeat_at"] > now:
                return
            active = db.execute("SELECT state FROM trades WHERE state IN ('sent','open','settling')").fetchone()
            if self._realized(db) <= -PAPER_LOSS_CAP:
                allowed, reason = False, "loss_cap_reached"
            state = ("error" if error else active[0] if active else "loss_limit" if reason == "loss_cap_reached"
                     else "waiting" if allowed else "standby")
            db.execute("""INSERT INTO worker_status VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                heartbeat_at=excluded.heartbeat_at,entries_allowed=excluded.entries_allowed,
                state=excluded.state,reason=excluded.reason""", (now, allowed, state, reason))
            if previous is None or (allowed, reason, error) != (previous["entries_allowed"], previous["reason"],
                                                               previous["state"] == "error"):
                kind = "entry_gate" if previous is None or allowed != previous["entries_allowed"] else "status"
                self._event(db, kind, now, {"entries_allowed": bool(allowed), "state": state, "reason": reason})

    def used(self, window_start):
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM trades WHERE window_start=?", (window_start,)).fetchone() is not None

    def skip(self, slug, signal_at, now, reason):
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO entry_skips VALUES(?,?,?,?)", (slug, signal_at, now, reason))

    def send(self, window, signal_at, now, payload):
        """Reserve the only pending order and this window's attempt before matching it."""
        with self._live_gate(lock=True) as (paused_at, _):
            if paused_at is None:
                return False
            with self._write(timeout=.15) as db:
                realized = self._realized(db)
                cap = min(PAPER_ENTRY_CAP, STARTING_BUDGET + realized, PAPER_LOSS_CAP + min(realized, ZERO))
                reserve = Decimal(str(payload["reserved_amount"]))
                if not reserve.is_finite() or not ZERO < reserve <= cap or not now < window.end:
                    return False
                payload = {**payload, "live_paused_at_send": paused_at}
                saved = db.execute("""INSERT OR IGNORE INTO trades
                    (slug,window_start,window_end,signal_at,sent_at,updated_at,state,payload)
                    VALUES(?,?,?,?,?,?,'sent',?)""", (window.slug, window.start, window.end, signal_at, now, now,
                                                     _dump(payload))).rowcount == 1
                if saved:
                    self._event(db, "entry_sent", now, _trade_event(window.slug, "sent", payload))
                return saved

    def update(self, trade, now, state, payload):
        if state not in TRANSITIONS.get(trade["state"], ()):
            raise ValueError("Invalid Oil D paper trade state")
        if now <= trade["updated_at"]:
            return False
        if trade["state"] == "sent":
            with self._live_gate(lock=True) as (paused_at, reason):
                if paused_at is None or trade["payload"].get("live_paused_at_send", paused_at) != paused_at:
                    state = "no_fill"
                    payload = {**trade["payload"], "entry": {"outcome": "canceled", "reason": reason or "live_pause_changed",
                                                            "filled_quantity": ZERO}}
                return self._update(trade, now, state, payload, timeout=.15)
        return self._update(trade, now, state, payload)

    def _update(self, trade, now, state, payload, timeout=2):
        with self._write(timeout) as db:
            closed_at = None if state in ACTIVE else now
            saved = db.execute("""UPDATE trades SET updated_at=?,closed_at=?,state=?,payload=?
                WHERE slug=? AND updated_at=? AND state=? AND closed_at IS NULL""",
                (now, closed_at, state, _dump(payload), trade["slug"], trade["updated_at"], trade["state"]))
            if saved.rowcount and state == "closed":
                net = Decimal(str(payload["net_pnl"]))
                db.execute("UPDATE account SET realized=? WHERE id=1", (str(self._realized(db) + net),))
            if saved.rowcount:
                for kind, stamp, evidence in _trade_events(trade, now, state, payload):
                    self._event(db, kind, stamp, evidence)
            return saved.rowcount == 1


def _trade_event(slug, state, data):
    return {"slug": slug, "side": data.get("side"), "state": state,
            "requested_quantity": data.get("quantity"), "limit_price": data.get("limit"),
            "remaining": data.get("remaining"), "coverage": data.get("coverage")}


def _trade_events(trade, now, state, data):
    base = _trade_event(trade["slug"], state, data)
    if trade["state"] == "sent":
        entry = data.get("entry", {})
        outcome = entry.get("outcome")
        kind = ("entry_partial" if outcome == "partial" else "entry_filled" if state == "open"
                else "entry_unverified" if state == "unverified" else "entry_unfilled")
        yield kind, now, {**base, "outcome": outcome, "reason": entry.get("reason"),
                          "quantity": entry.get("filled_quantity"), "price": entry.get("average_price"),
                          "fee": entry.get("fee"), "gross": entry.get("gross")}

    prior = len(trade["payload"].get("exit_events", ()))
    for event in data.get("exit_events", ())[prior:]:
        action, match = event["kind"], event.get("match")
        snapshot = {**base, "exit_kind": action, "quantity": event.get("quantity"), "limit_price": event.get("limit"),
                    "proceeds": event.get("proceeds")}
        kind = "settlement" if action == "settled" else "state_changed"
        if match is not None:
            kind = "exit_filled" if Decimal(str(match["filled_quantity"])) > ZERO else "exit_unfilled"
            snapshot.update(outcome=match.get("outcome"), reason=match.get("reason"), price=match.get("average_price"),
                            fee=match.get("fee"), gross=match.get("gross"))
        yield kind, event["at"], snapshot
    if state == "settling" and state != trade["state"]:
        yield "state_changed", now, {**base, "reason": "awaiting_official_settlement"}
    if state == "closed":
        yield "trade_closed", now, {**base, "net_pnl": data["net_pnl"]}


def events(path, after=0, limit=50):
    result = {"events": [], "latest_id": 0}
    path = Path(path)
    if not path.is_file():
        return result
    with readonly(path) as db:
        db.execute("BEGIN")
        if not _has_table(db, "events"):
            return result
        result["latest_id"] = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
        result["events"] = [{**json.loads(data), "id": key, "at": _iso(at), "kind": kind}
                            for key, at, kind, data in db.execute("""SELECT id,at,kind,payload FROM events
                             WHERE id>? ORDER BY id LIMIT ?""", (after, limit))]
    return result


def report(path, since, now, *, detail=False):
    result = {"started_at": None, "starting_budget": STARTING_BUDGET, "entry_cap": PAPER_ENTRY_CAP,
              "loss_cap": PAPER_LOSS_CAP,
              "realized_pnl": ZERO, "balance": STARTING_BUDGET, "period_net": ZERO, "buying_power": STARTING_BUDGET,
              "loss_guard": False, "active": None, "states": {}, "entry_outcomes": {}, "skip_reasons": {},
              "pnl_with_coverage_gap": ZERO, "closed_with_coverage_gap": 0, "closed_in_period": 0,
              "last_heartbeat": None, "entries_allowed": False, "worker_state": None, "worker_reason": None,
              "latest_id": 0}
    if not path.is_file():
        return result
    with readonly(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if not all(_has_table(db, table) for table in ("account", "trades", "entry_skips")):
            return result
        account = db.execute("SELECT started_at,realized FROM account WHERE id=1").fetchone()
        if account is None:
            return result
        if _has_table(db, "worker_status"):
            status = db.execute("SELECT * FROM worker_status WHERE id=1").fetchone()
            if status is not None:
                result.update(last_heartbeat=_iso(status["heartbeat_at"]), entries_allowed=bool(status["entries_allowed"]),
                              worker_state=status["state"], worker_reason=status["reason"])
        if _has_table(db, "events"):
            result["latest_id"] = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
        statement = "SELECT * FROM trades"
        rows = [_trade(row) for row in db.execute(statement + " WHERE sent_at BETWEEN ? AND ? ORDER BY sent_at",
                                                (since, now))]
        closes = [_trade(row) for row in db.execute(statement + " WHERE state='closed' AND closed_at BETWEEN ? AND ?",
                                                   (since, now))]
        active = _trade(db.execute(statement + " WHERE state IN ('sent','open','settling')").fetchone())
        skips = dict(db.execute("SELECT reason,count(*) FROM entry_skips WHERE at BETWEEN ? AND ? GROUP BY reason",
                               (since, now)))
    realized = Decimal(account["realized"])
    balance = STARTING_BUDGET + realized
    reserved = ZERO
    if active:
        data = active["payload"]
        entry = data.get("entry")
        reserved = (Decimal(entry["gross"]) + Decimal(entry["fee"]) - Decimal(data.get("net_exit_proceeds", "0"))
                    if entry else Decimal(data["reserved_amount"]))
        result["active"] = {"slug": active["slug"], "state": active["state"], "side": data["side"],
                            "remaining": data.get("remaining"), "coverage": data.get("coverage")}
    net = lambda row: Decimal(row["payload"]["net_pnl"])
    gapped = [row for row in closes if row["payload"].get("coverage")]
    result.update(started_at=account["started_at"], realized_pnl=realized, balance=balance,
                  buying_power=balance - reserved, loss_guard=realized <= -PAPER_LOSS_CAP,
                  states=dict(Counter(row["state"] for row in rows)),
                  entry_outcomes=dict(Counter((row["payload"].get("entry") or {}).get("outcome", row["state"])
                                              for row in rows)), skip_reasons=skips,
                  period_net=sum(map(net, closes), ZERO), pnl_with_coverage_gap=sum(map(net, gapped), ZERO),
                  closed_with_coverage_gap=len(gapped), closed_in_period=len(closes))
    if detail:
        result["trades"] = rows
    return result
