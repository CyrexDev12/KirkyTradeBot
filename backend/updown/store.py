"""SQLite journal for an Up/Down paper account (crypto and gold each keep their own file).

Rows are keyed by market key (``btc-15m``, ``gold-1h``) and window start, so the
gold 15-minute and hourly windows that start at the same time stay separate.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

PHOENIX = ZoneInfo("America/Phoenix")
STARTING_BALANCE = 100.0


def data_directory() -> Path:
    default = Path(__file__).resolve().parents[2] / "data"
    return Path(os.getenv("NFL_DATA_DIR", default)) / "updown"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(PHOENIX).date().isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS trades(
                    id INTEGER PRIMARY KEY, asset TEXT NOT NULL, window_start INTEGER NOT NULL,
                    status TEXT NOT NULL, cost REAL NOT NULL, net_pnl REAL NOT NULL DEFAULT 0,
                    closed_day TEXT, payload TEXT NOT NULL, UNIQUE(asset, window_start));
                CREATE TABLE IF NOT EXISTS windows(
                    asset TEXT NOT NULL, window_start INTEGER NOT NULL, price_to_beat REAL,
                    settlement REAL, outcome TEXT, last_reason TEXT, last_at TEXT,
                    PRIMARY KEY(asset, window_start));
                CREATE TABLE IF NOT EXISTS observations(
                    asset TEXT NOT NULL, window_start INTEGER NOT NULL, minute INTEGER NOT NULL,
                    seconds_left REAL, index_price REAL, coinbase REAL, fair_up REAL,
                    yes_bid REAL, yes_ask REAL, PRIMARY KEY(asset, window_start, minute));
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, kind TEXT NOT NULL,
                    at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(trade_id, kind));
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

    # ---- balances -------------------------------------------------------
    @staticmethod
    def _realized(db) -> float:
        return STARTING_BALANCE + db.execute("SELECT coalesce(sum(net_pnl),0) FROM trades WHERE status='CLOSED'"
                                             ).fetchone()[0]

    @staticmethod
    def _open_stake(db) -> float:
        return db.execute("SELECT coalesce(sum(cost),0) FROM trades WHERE status='OPEN'").fetchone()[0]

    def snapshot(self, now: float) -> dict:
        with self.transaction() as db:
            today = db.execute("SELECT coalesce(sum(net_pnl),0) FROM trades WHERE status='CLOSED' AND closed_day=?",
                               (local_day(now),)).fetchone()[0]
            return {"balance": self._realized(db), "open_stake": self._open_stake(db), "today_pnl": today}

    # ---- windows / observations ---------------------------------------
    def note_window(self, asset, start, reason, now, price_to_beat=None):
        with self.transaction(True) as db:
            db.execute("""INSERT INTO windows(asset,window_start,price_to_beat,last_reason,last_at) VALUES(?,?,?,?,?)
                          ON CONFLICT(asset,window_start) DO UPDATE SET last_reason=excluded.last_reason,
                          last_at=excluded.last_at, price_to_beat=coalesce(excluded.price_to_beat, price_to_beat)""",
                       (asset, start, price_to_beat, reason, iso(now)))

    def observe(self, asset, start, now, **values):
        with self.transaction(True) as db:
            db.execute("""INSERT OR IGNORE INTO observations(asset,window_start,minute,seconds_left,index_price,
                          coinbase,fair_up,yes_bid,yes_ask) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (asset, start, int((now - start) // 60), values.get("seconds_left"), values.get("index_price"),
                        values.get("coinbase"), values.get("fair_up"), values.get("yes_bid"), values.get("yes_ask")))

    def record_outcome(self, asset, start, price_to_beat, settlement):
        outcome = "UNKNOWN" if settlement is None else ("UP" if settlement >= price_to_beat else "DOWN")
        with self.transaction(True) as db:
            db.execute("""INSERT INTO windows(asset,window_start,price_to_beat,settlement,outcome) VALUES(?,?,?,?,?)
                          ON CONFLICT(asset,window_start) DO UPDATE SET settlement=excluded.settlement,
                          outcome=excluded.outcome, price_to_beat=coalesce(price_to_beat, excluded.price_to_beat)""",
                       (asset, start, price_to_beat, settlement, outcome))
        return outcome

    def unresolved_windows(self, before: int) -> list[tuple[str, int, float | None]]:
        with self.transaction() as db:
            return [(r[0], r[1], r[2]) for r in db.execute(
                "SELECT asset,window_start,price_to_beat FROM windows WHERE outcome IS NULL AND window_start+300<=?"
                " ORDER BY window_start LIMIT 20", (before,))]

    # ---- trades ---------------------------------------------------------
    @staticmethod
    def _event(db, kind, trade, now):
        db.execute("INSERT OR IGNORE INTO events(trade_id,kind,at,payload) VALUES(?,?,?,?)",
                   (trade["id"], kind, iso(now), json.dumps(trade, allow_nan=False)))

    def held(self, asset, start) -> bool:
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM trades WHERE asset=? AND window_start=?", (asset, start)).fetchone() is not None

    def open_trades(self) -> list[dict]:
        with self.transaction() as db:
            return [{**json.loads(r["payload"]), "id": r["id"]} for r in
                    db.execute("SELECT id,payload FROM trades WHERE status='OPEN' ORDER BY id")]

    def open(self, window, decision, now, extra=None) -> dict | None:
        trade = {"mode": "PAPER_ONLY", "status": "OPEN", "asset": window.asset, "key": window.key,
                 "horizon": window.horizon, "market_slug": window.slug,
                 "window_start": iso(window.start), "window_end": iso(window.end), "window_start_ts": window.start,
                 "window_end_ts": window.end, "price_to_beat": window.price_to_beat, "theta": window.theta,
                 "side": decision.side, "quantity": decision.quantity, "remaining": decision.quantity,
                 "entry_price": round(decision.price, 6), "entry_fee": round(decision.fee, 6),
                 "cost": round(decision.cost, 6), "fair_up": decision.fair_up, "fair_side": decision.fair_side,
                 "edge": decision.edge, "reason": decision.reason, "opened_at": iso(now), "exits": [],
                 **decision.risk, **(extra or {})}
        with self.transaction(True) as db:
            # Re-check limits inside the write lock so two ticks cannot overspend.
            if db.execute("SELECT 1 FROM trades WHERE asset=? AND window_start=?", (window.key, window.start)).fetchone():
                return None
            balance = self._realized(db)
            trade["balance_at_entry"] = round(balance, 6)
            cur = db.execute("INSERT INTO trades(asset,window_start,status,cost,payload) VALUES(?,?,?,?,?)",
                             (window.key, window.start, "OPEN", trade["cost"], json.dumps(trade, allow_nan=False)))
            trade["id"] = cur.lastrowid
            self._event(db, "opened", trade, now)
        return trade

    def _save(self, db, trade):
        db.execute("UPDATE trades SET payload=? WHERE id=?", (json.dumps(trade, allow_nan=False), trade["id"]))

    def partial_exit(self, trade_id, exit_data, now) -> dict:
        with self.transaction(True) as db:
            row = db.execute("SELECT payload FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
            if not row:
                return None
            trade = {**json.loads(row[0]), "id": trade_id}
            take = min(exit_data["quantity"], trade["remaining"])
            if take <= 0:
                return None
            scale = take / exit_data["quantity"]
            leg = {"kind": "take_profit", "at": iso(now), "quantity": take, "price": round(exit_data["price"], 6),
                   "proceeds": round(exit_data["gross"] * scale, 6), "fee": round(exit_data["fee"] * scale, 6),
                   "fair_side": exit_data["fair_side"]}
            trade["exits"].append(leg)
            trade["remaining"] -= take
            if trade["remaining"] == 0:
                self._close(db, trade, now, "take_profit")
            else:
                self._save(db, trade)
            return trade

    def _close(self, db, trade, now, how, settlement=None, outcome=None):
        received = sum(x["proceeds"] - x["fee"] for x in trade["exits"])
        if outcome is not None and trade["remaining"]:
            payout = float(trade["remaining"]) if outcome == trade["side"] else 0.0
            trade["exits"].append({"kind": "settlement", "at": iso(now), "quantity": trade["remaining"],
                                   "price": 1.0 if payout else 0.0, "proceeds": payout, "fee": 0.0})
            received += payout
            trade["remaining"] = 0
        trade.update({"status": "CLOSED", "closed_at": iso(now), "exit_reason": how, "settlement_price": settlement,
                      "outcome": outcome, "proceeds": round(received, 6),
                      "net_pnl": round(received - trade["cost"], 6)})
        db.execute("UPDATE trades SET status='CLOSED', net_pnl=?, closed_day=?, payload=? WHERE id=?",
                   (trade["net_pnl"], local_day(now), json.dumps(trade, allow_nan=False), trade["id"]))
        trade["record"] = self._record(db)
        trade["balance_after"] = round(self._realized(db), 6)
        self._event(db, "closed", trade, now)

    def settle(self, trade_id, settlement, now, source="polymarket") -> dict | None:
        with self.transaction(True) as db:
            row = db.execute("SELECT payload FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
            if not row:
                return None
            trade = {**json.loads(row[0]), "id": trade_id}
            outcome = "UP" if settlement >= trade["price_to_beat"] else "DOWN"
            trade["settlement_source"] = source
            self._close(db, trade, now, "settlement", settlement, outcome)
            return trade

    @staticmethod
    def _record(db) -> dict:
        rows = [r[0] for r in db.execute("SELECT net_pnl FROM trades WHERE status='CLOSED'")]
        return {"wins": sum(x > 0 for x in rows), "losses": sum(x < 0 for x in rows), "flat": sum(x == 0 for x in rows)}

    # ---- read side ------------------------------------------------------
    def account(self, now: float | None = None) -> dict:
        snap = self.snapshot(now or datetime.now(timezone.utc).timestamp())
        with self.transaction() as db:
            latest = db.execute("SELECT asset,window_start,last_reason,last_at FROM windows "
                                "WHERE last_reason IS NOT NULL ORDER BY last_at DESC LIMIT 3").fetchall()
            return {"mode": "PAPER_ONLY", "venue": "Polymarket US crypto Up/Down 15m (simulated fills)",
                    "starting_balance": STARTING_BALANCE, "balance": round(snap["balance"], 6),
                    "open_stake": round(snap["open_stake"], 6),
                    "available_cash": round(snap["balance"] - snap["open_stake"], 6),
                    "realized_pnl": round(snap["balance"] - STARTING_BALANCE, 6), "today_pnl": round(snap["today_pnl"], 6),
                    "open_trades": [{**json.loads(r["payload"]), "id": r["id"]} for r in
                                    db.execute("SELECT id,payload FROM trades WHERE status='OPEN'")],
                    "record": self._record(db),
                    "latest_decisions": [{"asset": r[0], "window_start": iso(r[1]), "reason": r[2], "at": r[3]}
                                         for r in latest],
                    "latest_id": db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]}

    def trades(self, limit: int) -> list[dict]:
        with self.transaction() as db:
            return [{**json.loads(r["payload"]), "id": r["id"]}
                    for r in db.execute("SELECT id,payload FROM trades ORDER BY id DESC LIMIT ?", (limit,))]

    def stats(self) -> dict:
        with self.transaction() as db:
            closed = [json.loads(r[0]) for r in db.execute("SELECT payload FROM trades WHERE status='CLOSED'")]
            obs = db.execute("""SELECT o.fair_up, w.outcome, o.yes_ask, o.yes_bid FROM observations o JOIN windows w
                                ON w.asset=o.asset AND w.window_start=o.window_start
                                WHERE w.outcome IN ('UP','DOWN') AND o.fair_up IS NOT NULL""").fetchall()
            windows = db.execute("SELECT count(*) FROM windows WHERE outcome IN ('UP','DOWN')").fetchone()[0]
        pnl = [t["net_pnl"] for t in closed]
        staked = sum(t["cost"] for t in closed)
        by_asset = {}
        for t in closed:
            a = by_asset.setdefault(t.get("key", t["asset"]), {"trades": 0, "wins": 0, "net_pnl": 0.0})
            a["wins"] += t["net_pnl"] > 0
            a["trades"] += 1
            a["net_pnl"] = round(a["net_pnl"] + t["net_pnl"], 6)

        def brier(pairs):
            pairs = [(p, 1.0 if o == "UP" else 0.0) for p, o in pairs if p is not None]
            return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 4) if pairs else None

        mids = [((a + b) / 2, o) for _, o, a, b in obs if a is not None and b is not None]
        return {"mode": "PAPER_ONLY", "closed_trades": len(closed), "wins": sum(x > 0 for x in pnl),
                "losses": sum(x < 0 for x in pnl), "net_pnl": round(sum(pnl), 6), "staked": round(staked, 6),
                "return_on_stake": round(sum(pnl) / staked, 4) if staked else None, "by_asset": by_asset,
                "windows_resolved": windows, "observations": len(obs),
                "model_brier": brier([(p, o) for p, o, _, _ in obs]), "market_mid_brier": brier(mids)}

    def events(self, after: int, limit: int) -> dict:
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
            return {"events": [{"id": r["id"], "kind": r["kind"], "at": r["at"], "trade": json.loads(r["payload"])}
                               for r in rows],
                    "latest_id": db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]}
