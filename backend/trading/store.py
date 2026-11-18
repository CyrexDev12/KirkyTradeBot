"""SQLite persistence for the paper-trading subsystem.

One writer, explicit transactions. The ledger is an append-only event journal
(``ledger_events``) with unique event IDs; orders, fills, lots, reservations and
account balances are materialized from it inside the same transaction that
appends the events, so a crash can never leave a fill without its inventory,
fee, reservation release or notification.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from .common import ZERO, D, dumps, iso
from .config import StrategyConfig
from .paper_engine import ACTIVE_ENTRY, PaperEngine

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_versions (version TEXT PRIMARY KEY, strategy_id TEXT, config TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS model_runs (forecast_id TEXT PRIMARY KEY, provider TEXT, game_id TEXT, model_version TEXT,
    as_of TEXT, payload TEXT, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS market_contracts (market_slug TEXT, selected_outcome TEXT, game_id TEXT, season INTEGER,
    week INTEGER, market_type TEXT, payload TEXT, verified_at TEXT, PRIMARY KEY (market_slug, selected_outcome));
CREATE TABLE IF NOT EXISTS market_snapshots (id INTEGER PRIMARY KEY, slug TEXT, received_at TEXT, exchange_ts TEXT,
    sequence INTEGER, state TEXT, source TEXT, payload TEXT, quality_flags TEXT, UNIQUE (slug, received_at, source));
CREATE INDEX IF NOT EXISTS snapshots_by_slug ON market_snapshots(slug, received_at);
CREATE TABLE IF NOT EXISTS recorded_trades (id INTEGER PRIMARY KEY, slug TEXT, ts TEXT, price TEXT, quantity TEXT,
    source TEXT, received_at TEXT, UNIQUE (slug, ts, price, quantity, source));
CREATE TABLE IF NOT EXISTS market_events (id INTEGER PRIMARY KEY, slug TEXT, game_id TEXT, kind TEXT, at TEXT,
    payload TEXT);
CREATE TABLE IF NOT EXISTS candidate_decisions (id INTEGER PRIMARY KEY, plan_key TEXT, game_id TEXT, market_slug TEXT,
    selected_outcome TEXT, accepted INTEGER, rejection_codes TEXT, payload TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS weekly_plans (plan_key TEXT PRIMARY KEY, strategy_id TEXT, season INTEGER, week INTEGER,
    config_version TEXT, as_of TEXT, status TEXT, summary TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS plan_revisions (id INTEGER PRIMARY KEY, plan_key TEXT, trade_id TEXT, revision INTEGER,
    reason TEXT, payload TEXT, created_at TEXT, UNIQUE (trade_id, revision));
CREATE TABLE IF NOT EXISTS trade_plans (trade_id TEXT PRIMARY KEY, plan_key TEXT, revision INTEGER, status TEXT,
    payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ledger_events (seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, type TEXT,
    trade_id TEXT, ts TEXT, payload TEXT, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS engine_memory (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_orders (order_id TEXT PRIMARY KEY, trade_id TEXT, status TEXT, quantity TEXT,
    filled TEXT, limit_price TEXT, reserved TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_fills (event_id TEXT PRIMARY KEY, trade_id TEXT, kind TEXT, reason TEXT, ts TEXT,
    quantity TEXT, price TEXT, gross TEXT, fee TEXT, net TEXT);
CREATE TABLE IF NOT EXISTS position_lots (trade_id TEXT PRIMARY KEY, status TEXT, qty_open TEXT, qty_bought TEXT,
    cost_basis TEXT, realized TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS cash_reservations (order_id TEXT PRIMARY KEY, amount TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS account_snapshots (id INTEGER PRIMARY KEY, kind TEXT, label TEXT, as_of TEXT, equity TEXT,
    payload TEXT, provisional INTEGER, revision_of INTEGER, created_at TEXT, UNIQUE (kind, label));
CREATE TABLE IF NOT EXISTS validation_runs (id INTEGER PRIMARY KEY, strategy_version TEXT, created_at TEXT,
    passed INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS path_examples (id INTEGER PRIMARY KEY, game_id TEXT, season INTEGER, week INTEGER,
    bucket TEXT, label TEXT, payload TEXT, origin TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS scheduled_jobs (job_key TEXT PRIMARY KEY, kind TEXT, status TEXT, lease_until TEXT,
    attempts INTEGER DEFAULT 0, detail TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS slate_observations (game_id TEXT PRIMARY KEY, final_first_seen TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS discord_outbox (publication_id TEXT PRIMARY KEY, kind TEXT, channel TEXT, revision INTEGER,
    payload TEXT, created_at TEXT, status TEXT DEFAULT 'pending', message_id TEXT, delivered_at TEXT, error TEXT);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def tx(self):
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # ------------------------------------------------------------- engine I/O
    def load_engine(self, cfg: StrategyConfig, fees=None) -> PaperEngine:
        engine = PaperEngine(cfg, fees)
        with closing(self.connect()) as db:
            for row in db.execute("SELECT payload FROM ledger_events ORDER BY seq"):
                engine.apply(json.loads(row["payload"]))
            mem = db.execute("SELECT payload FROM engine_memory WHERE id=1").fetchone()
            if mem:
                engine.memory = json.loads(mem["payload"])
        return engine

    def commit(self, engine: PaperEngine, now: datetime, outbox: list[dict] | None = None,
               extra=None, write_memory: bool = False) -> list[dict]:
        """Atomically append engine events, refresh materialized views and the outbox.

        Only the process that feeds market snapshots (the worker, a replay or the
        mock run) passes ``write_memory=True``; other writers (API operator
        commands, the planner) append events without overwriting the liquidity
        and mark memory with a stale copy.
        """
        events = engine.take_events()
        with self.tx() as db:
            for e in events:
                db.execute("INSERT OR IGNORE INTO ledger_events (event_id,type,trade_id,ts,payload,recorded_at) "
                           "VALUES (?,?,?,?,?,?)", (e["event_id"], e["type"], e.get("trade_id"), e["ts"], dumps(e),
                                                   iso(now)))
                if e["type"] in ("ENTRY_FILL", "EXIT_FILL", "SETTLED"):
                    price = e.get("price") or e.get("payout")
                    db.execute("INSERT OR IGNORE INTO paper_fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (e["event_id"], e["trade_id"], e["type"], e.get("reason"), e["ts"], e["quantity"],
                                price, e.get("gross") or e.get("cost"), e.get("fee"),
                                e.get("net_proceeds") or e.get("cost")))
            self._materialize(db, engine, now)
            if write_memory:
                db.execute("INSERT OR REPLACE INTO engine_memory VALUES (1,?,?)", (dumps(engine.memory), iso(now)))
            for item in outbox or []:
                self._enqueue(db, item, now)
            if extra:
                extra(db)
        return events

    def _materialize(self, db, engine: PaperEngine, now):
        s = engine.state
        for o in s.orders.values():
            db.execute("INSERT OR REPLACE INTO paper_orders VALUES (?,?,?,?,?,?,?,?)",
                       (o.order_id, o.trade_id, o.status, str(o.quantity), str(o.filled), str(o.limit),
                        str(o.reserved), iso(now)))
            if o.status in ACTIVE_ENTRY:
                db.execute("INSERT OR REPLACE INTO cash_reservations VALUES (?,?,?)", (o.order_id, str(o.reserved), iso(now)))
            else:
                db.execute("DELETE FROM cash_reservations WHERE order_id=?", (o.order_id,))
        for p in s.positions.values():
            db.execute("INSERT OR REPLACE INTO position_lots VALUES (?,?,?,?,?,?,?)",
                       (p.trade_id, p.status, str(p.qty_open), str(p.qty_bought), str(p.cost_basis), str(p.realized),
                        iso(now)))

    def ledger_events(self, since_seq: int = 0) -> list[dict]:
        with closing(self.connect()) as db:
            return [json.loads(r["payload"]) | {"seq": r["seq"]} for r in
                    db.execute("SELECT seq, payload FROM ledger_events WHERE seq>? ORDER BY seq", (since_seq,))]

    # ----------------------------------------------------------------- outbox
    def _enqueue(self, db, item: dict, now):
        db.execute("""INSERT INTO discord_outbox (publication_id,kind,channel,revision,payload,created_at)
                      VALUES (?,?,?,?,?,?) ON CONFLICT(publication_id) DO UPDATE SET
                      payload=excluded.payload, revision=excluded.revision,
                      status=CASE WHEN discord_outbox.revision < excluded.revision THEN 'pending'
                                  ELSE discord_outbox.status END
                      WHERE discord_outbox.revision < excluded.revision""",
                   (item["publication_id"], item["kind"], item["channel"], item.get("revision", 1),
                    dumps(item["payload"]), iso(now)))

    def enqueue(self, items: list[dict], now):
        with self.tx() as db:
            for item in items:
                self._enqueue(db, item, now)

    def pending_publications(self, limit=20) -> list[dict]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM discord_outbox WHERE status='pending' ORDER BY created_at, publication_id "
                              "LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def ack_publication(self, publication_id: str, revision: int, message_id: str | None, error: str | None, now):
        with self.tx() as db:
            if error:
                db.execute("UPDATE discord_outbox SET error=? WHERE publication_id=? AND revision=?",
                           (error, publication_id, revision))
            else:
                db.execute("UPDATE discord_outbox SET status='delivered', message_id=?, delivered_at=?, error=NULL "
                           "WHERE publication_id=? AND revision=?", (message_id, iso(now), publication_id, revision))

    def publication(self, publication_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM discord_outbox WHERE publication_id=?", (publication_id,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    # ------------------------------------------------------- market recording
    def record_snapshot(self, snap) -> bool:
        data = snap.to_dict()
        with self.tx() as db:
            cur = db.execute("INSERT OR IGNORE INTO market_snapshots (slug,received_at,exchange_ts,sequence,state,source,"
                             "payload,quality_flags) VALUES (?,?,?,?,?,?,?,?)",
                             (snap.slug, data["received_at"], data["exchange_ts"], snap.sequence, snap.state,
                              snap.source, dumps(data), dumps(data["quality_flags"])))
            return cur.rowcount == 1

    def snapshots(self, slug: str, start: str | None = None, end: str | None = None, source: str | None = None):
        from .book import BookSnapshot
        q, args = "SELECT payload FROM market_snapshots WHERE slug=?", [slug]
        if start:
            q, args = q + " AND received_at>=?", args + [start]
        if end:
            q, args = q + " AND received_at<=?", args + [end]
        if source:
            q, args = q + " AND source=?", args + [source]
        with closing(self.connect()) as db:
            return [BookSnapshot.from_dict(json.loads(r["payload"])) for r in db.execute(q + " ORDER BY received_at", args)]

    def record_market_event(self, slug, game_id, kind, at, payload):
        with self.tx() as db:
            db.execute("INSERT INTO market_events (slug,game_id,kind,at,payload) VALUES (?,?,?,?,?)",
                       (slug, game_id, kind, iso(at), dumps(payload)))

    def record_contract(self, spec, now):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO market_contracts VALUES (?,?,?,?,?,?,?,?)",
                       (spec.market_slug, spec.selected_outcome, spec.game_id, spec.season, spec.week,
                        spec.market_type, dumps(spec.to_dict()), iso(now)))

    # ------------------------------------------------------------ plans/jobs
    def weekly_plan(self, plan_key: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM weekly_plans WHERE plan_key=?", (plan_key,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"]), "summary": json.loads(row["summary"])} if row else None

    def latest_plan(self, strategy_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT plan_key FROM weekly_plans WHERE strategy_id=? ORDER BY season DESC, week DESC "
                             "LIMIT 1", (strategy_id,)).fetchone()
        return self.weekly_plan(row["plan_key"]) if row else None

    def trade_plan(self, trade_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM trade_plans WHERE trade_id=?", (trade_id,)).fetchone()
            if not row:
                return None
            revisions = [dict(r) | {"payload": json.loads(r["payload"])} for r in
                         db.execute("SELECT * FROM plan_revisions WHERE trade_id=? ORDER BY revision", (trade_id,))]
            events = [json.loads(r["payload"]) for r in
                      db.execute("SELECT payload FROM ledger_events WHERE trade_id=? ORDER BY seq", (trade_id,))]
        return {**dict(row), "payload": json.loads(row["payload"]), "revisions": revisions, "events": events}

    def job(self, job_key: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE job_key=?", (job_key,)).fetchone()
        return dict(row) if row else None

    def set_job(self, job_key, kind, status, now, detail=None, db=None):
        sql = ("INSERT INTO scheduled_jobs (job_key,kind,status,attempts,detail,updated_at) VALUES (?,?,?,1,?,?) "
               "ON CONFLICT(job_key) DO UPDATE SET status=excluded.status, attempts=scheduled_jobs.attempts+1, "
               "detail=excluded.detail, updated_at=excluded.updated_at")
        args = (job_key, kind, status, dumps(detail) if detail is not None else None, iso(now))
        if db is not None:
            db.execute(sql, args)
        else:
            with self.tx() as conn:
                conn.execute(sql, args)

    def observe_final(self, game_id: str, now, payload) -> str:
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO slate_observations VALUES (?,?,?)", (game_id, iso(now), dumps(payload)))
            return db.execute("SELECT final_first_seen FROM slate_observations WHERE game_id=?",
                              (game_id,)).fetchone()[0]

    def save_account_snapshot(self, kind: str, label: str, account: dict, now) -> bool:
        """Immutable: a second snapshot under the same label is stored as a labeled revision."""
        with self.tx() as db:
            existing = db.execute("SELECT id FROM account_snapshots WHERE kind=? AND label=?", (kind, label)).fetchone()
            if existing:
                n = db.execute("SELECT COUNT(*) FROM account_snapshots WHERE revision_of=?", (existing[0],)).fetchone()[0]
                label, revision_of = f"{label}#rev{n + 1}", existing[0]
            else:
                revision_of = None
            db.execute("INSERT INTO account_snapshots (kind,label,as_of,equity,payload,provisional,revision_of,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (kind, label, account["as_of"], account["equity"], dumps(account),
                                                    int(bool(account.get("provisional"))), revision_of, iso(now)))
            return revision_of is None

    def account_snapshots(self, kind: str | None = None) -> list[dict]:
        with closing(self.connect()) as db:
            q = "SELECT * FROM account_snapshots" + (" WHERE kind=?" if kind else "") + " ORDER BY as_of, id"
            rows = db.execute(q, (kind,) if kind else ()).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def high_water(self, starting: Decimal, since: str | None = None) -> Decimal:
        """Highest non-provisional snapshot equity, optionally only since a recorded review."""
        values = [D(r["equity"]) for r in self.account_snapshots()
                  if not r["provisional"] and (since is None or r["as_of"] >= since)]
        return max([starting if since is None else ZERO, *values])

    def save_validation(self, version: str, passed: bool, payload: dict, now):
        with self.tx() as db:
            db.execute("INSERT INTO validation_runs (strategy_version,created_at,passed,payload) VALUES (?,?,?,?)",
                       (version, iso(now), int(passed), dumps(payload)))

    def latest_validation(self, version: str | None = None) -> dict | None:
        with closing(self.connect()) as db:
            q = "SELECT * FROM validation_runs" + (" WHERE strategy_version=?" if version else "") + " ORDER BY id DESC LIMIT 1"
            row = db.execute(q, (version,) if version else ()).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def record_strategy_version(self, cfg: StrategyConfig, now):
        from dataclasses import asdict
        data = asdict(cfg)
        data.pop("extra", None)
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO strategy_versions VALUES (?,?,?,?)",
                       (cfg.version, cfg.strategy_id, dumps(data), iso(now)))

    def path_examples(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [json.loads(r["payload"]) for r in db.execute("SELECT payload FROM path_examples ORDER BY id")]

    def add_path_examples(self, examples: list[dict], now):
        with self.tx() as db:
            for ex in examples:
                db.execute("INSERT INTO path_examples (game_id,season,week,bucket,label,payload,origin,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?)", (ex["game_id"], ex["season"], ex["week"], ex["bucket"],
                                                        ex["label"], dumps(ex), ex.get("origin", "replay"), iso(now)))
