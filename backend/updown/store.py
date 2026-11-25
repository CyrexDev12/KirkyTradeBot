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

from .depth import available_depth, spend_depth

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
                CREATE TABLE IF NOT EXISTS execution_depth(
                    slug TEXT PRIMARY KEY, observed_at REAL NOT NULL, exchange_at REAL,
                    sequence INTEGER, spent TEXT NOT NULL);
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

    # ---- displayed depth ------------------------------------------------
    @staticmethod
    def _depth_row(db, slug):
        return db.execute("SELECT * FROM execution_depth WHERE slug=?", (slug,)).fetchone()

    def execution_book(self, slug, book):
        """Give matching the unspent depth from a non-regressing observation."""
        with self.transaction(True) as db:
            row = self._depth_row(db, slug)
            exchange_at, sequence = book.exchange_at, book.sequence
            if row:
                for name, value in (("observed_at", book.at), ("exchange_at", exchange_at), ("sequence", sequence)):
                    if value is not None and row[name] is not None and value < row[name]:
                        return None
            spent = json.loads(row["spent"]) if row else {"bids": {}, "offers": {}}
            adjusted = available_depth(book, spent)
            db.execute("""INSERT INTO execution_depth(slug,observed_at,exchange_at,sequence,spent)
                          VALUES(?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET observed_at=excluded.observed_at,
                          exchange_at=coalesce(excluded.exchange_at,exchange_at),
                          sequence=coalesce(excluded.sequence,sequence), spent=excluded.spent""",
                       (slug, book.at, exchange_at, sequence, json.dumps(spent)))
        return adjusted

    def _consume(self, db, slug, book, side, buying, fills, quantity, participation):
        """Revalidate and spend the levels in the same transaction as the fill."""
        row = self._depth_row(db, slug)
        if (not row or row["observed_at"] != book.at or not fills or sum(q for _, q in fills) != quantity
                or (book.exchange_at is not None and row["exchange_at"] != book.exchange_at)
                or (book.sequence is not None and row["sequence"] != book.sequence)):
            return False
        spent = json.loads(row["spent"])
        if not spend_depth(book, spent, side, buying, fills, participation):
            return False
        db.execute("UPDATE execution_depth SET spent=? WHERE slug=?", (json.dumps(spent), slug))
        return True

    @staticmethod
    def _book_evidence(book):
        return {"book_at": iso(book.at), "book_sequence": book.sequence,
                "book_requested_at": iso(book.requested_at) if book.requested_at is not None else None,
                "book_exchange_at": iso(book.exchange_at) if book.exchange_at is not None else None}

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

    @staticmethod
    def _record_outcome(db, asset, start, price_to_beat, settlement, outcome):
        db.execute("""INSERT INTO windows(asset,window_start,price_to_beat,settlement,outcome) VALUES(?,?,?,?,?)
                      ON CONFLICT(asset,window_start) DO UPDATE SET settlement=excluded.settlement,
                      outcome=excluded.outcome, price_to_beat=coalesce(excluded.price_to_beat, price_to_beat)""",
                   (asset, start, price_to_beat, settlement, outcome))

    def record_outcome(self, asset, start, price_to_beat, settlement, *, outcome):
        with self.transaction(True) as db:
            self._record_outcome(db, asset, start, price_to_beat, settlement, outcome)

    def unresolved_windows(self, before: dict[str, int]) -> list[tuple[str, int, float]]:
        # Open positions are checked separately. Calibration needs at least one priced observation.
        with self.transaction() as db:
            return [(r[0], r[1], r[2]) for asset, until in before.items() for r in db.execute(
                """SELECT w.asset,w.window_start,w.price_to_beat FROM windows w
                   LEFT JOIN trades t ON t.asset=w.asset AND t.window_start=w.window_start
                   WHERE w.asset=? AND w.window_start<=? AND (w.outcome IS NULL OR w.outcome='UNKNOWN')
                     AND w.price_to_beat IS NOT NULL AND (t.id IS NULL OR t.status!='OPEN')
                     AND EXISTS(SELECT 1 FROM observations o WHERE o.asset=w.asset
                         AND o.window_start=w.window_start AND o.fair_up IS NOT NULL)""", (asset, until))]

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

    def open(self, window, decision, now, extra=None, *, book=None, participation=0.5) -> dict | None:
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
            if book is not None:
                stake = self._open_stake(db)
                today = db.execute("SELECT coalesce(sum(net_pnl),0) FROM trades WHERE status='CLOSED' AND closed_day=?",
                                   (local_day(now),)).fetchone()[0]
                s = decision.risk
                budget = min(balance - stake, balance * s["max_stake_pct"], balance * s["max_open_stake_pct"] - stake)
                if (not window.start <= book.at <= now < window.end or decision.cost > budget + 1e-9
                        or today <= -s["daily_loss_limit"] * (balance - today)
                        or not self._consume(db, window.slug, book, decision.side, True, decision.fills,
                                             decision.quantity, participation)):
                    return None
                trade.update(self._book_evidence(book), entry_fills=decision.fills)
            trade["balance_at_entry"] = round(balance, 6)
            cur = db.execute("INSERT INTO trades(asset,window_start,status,cost,payload) VALUES(?,?,?,?,?)",
                             (window.key, window.start, "OPEN", trade["cost"], json.dumps(trade, allow_nan=False)))
            trade["id"] = cur.lastrowid
            self._event(db, "opened", trade, now)
        return trade

    def _save(self, db, trade):
        db.execute("UPDATE trades SET payload=? WHERE id=?", (json.dumps(trade, allow_nan=False), trade["id"]))

    def partial_exit(self, trade_id, exit_data, now, *, book=None, participation=0.5) -> dict | None:
        with self.transaction(True) as db:
            row = db.execute("SELECT payload FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
            if not row:
                return None
            trade = {**json.loads(row[0]), "id": trade_id}
            take = min(exit_data["quantity"], trade["remaining"])
            if take <= 0:
                return None
            if book is not None and (
                    not trade["window_start_ts"] <= book.at <= now < trade["window_end_ts"]
                    or not self._consume(db, trade["market_slug"], book, trade["side"], False,
                                         exit_data.get("fills"), take, participation)):
                return None
            scale = take / exit_data["quantity"]
            kind = exit_data.get("kind", "take_profit")
            leg = {"kind": kind, "at": iso(now), "quantity": take, "price": round(exit_data["price"], 6),
                   "proceeds": round(exit_data["gross"] * scale, 6), "fee": round(exit_data["fee"] * scale, 6),
                   "fair_side": exit_data["fair_side"]}
            if book is not None:
                leg.update(self._book_evidence(book), fills=exit_data["fills"])
                leg.update({key: exit_data[key] for key in ("signal_at", "order_ready_at", "trigger", "news")
                            if key in exit_data})
            trade["exits"].append(leg)
            trade["remaining"] -= take
            if trade["remaining"] == 0:
                self._close(db, trade, now, kind)
            else:
                self._save(db, trade)
            return trade

    def _close(self, db, trade, now, how, settlement=None, outcome=None, payout_per_contract=None):
        received = sum(x["proceeds"] - x["fee"] for x in trade["exits"])
        if payout_per_contract is not None and trade["remaining"]:
            payout = trade["remaining"] * payout_per_contract
            trade["exits"].append({"kind": "settlement", "at": iso(now), "quantity": trade["remaining"],
                                   "price": payout_per_contract, "proceeds": payout, "fee": 0.0})
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

    def settle(self, trade_id, settlement, now, source="polymarket", *, yes_payout=None,
               official_price_to_beat=None) -> dict | None:
        with self.transaction(True) as db:
            row = db.execute("SELECT payload FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
            if not row:
                return None
            trade = {**json.loads(row[0]), "id": trade_id}
            if yes_payout is None:
                yes_payout = float(settlement >= trade["price_to_beat"])
            if not 0 <= yes_payout <= 1:
                raise ValueError("invalid official settlement payout")
            outcome = "UP" if yes_payout == 1 else "DOWN" if yes_payout == 0 else "OFFICIAL_PAYOUT"
            payout = yes_payout if trade["side"] == "UP" else 1 - yes_payout
            if official_price_to_beat is not None:
                trade["official_price_to_beat"] = official_price_to_beat
            trade["settlement_source"] = source
            self._close(db, trade, now, "settlement", settlement, outcome, payout)
            self._record_outcome(db, trade["key"], trade["window_start_ts"],
                                 official_price_to_beat if official_price_to_beat is not None else trade["price_to_beat"],
                                 settlement, outcome)
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
