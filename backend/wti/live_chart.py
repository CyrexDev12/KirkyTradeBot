"""Build a realized Oil D account curve from the private live journal."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Literal

from .live_store import LiveStore


ChartTime = Literal["1h", "6h", "12h", "24h", "7d", "30d", "all"]
HOURS = {"1h": 1, "6h": 6, "12h": 12, "24h": 24, "7d": 168, "30d": 720}


def _iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def _pilot(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"control", "trades"}.issubset(tables):
        return None
    if active := LiveStore._active_pilot(db):
        return {**active, "budget_source": "pilot_run"}
    legacy = db.execute("""SELECT json_extract(payload,'$.pilot_budget') AS budget, heartbeat_at
                           FROM control WHERE id=1""").fetchone()
    if not legacy or legacy["budget"] is None or legacy["heartbeat_at"] is None:
        return None
    first_trade = db.execute("SELECT min(created_at) FROM trades").fetchone()[0]
    return {"starting_budget": legacy["budget"], "last_closed_trade_id": 0,
            "started_at": first_trade if first_trade is not None else legacy["heartbeat_at"],
            "budget_source": "legacy_heartbeat"}


def chart(path: Path, period: ChartTime, now: float) -> dict:
    if not path.is_file():
        return {"available": False, "message": "Waiting for the Oil D live journal."}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        pilot = _pilot(db)
        if pilot is None:
            return {"available": False, "message": "Waiting for the live worker to record its starting budget."}
        rows = db.execute("""
            SELECT closed_at, json_extract(payload,'$.net_pnl') AS net,
                   json_extract(payload,'$.fees_exact') AS exact
            FROM trades WHERE id>? AND closed_at<=? AND upper(state)='CLOSED'
                AND json_extract(payload,'$.net_pnl') IS NOT NULL
            ORDER BY closed_at,id""", (pilot["last_closed_trade_id"], now)).fetchall()
        open_trade = db.execute("SELECT 1 FROM trades WHERE state='active' LIMIT 1").fetchone() is not None
    budget = Decimal(str(pilot["starting_budget"]))
    start = pilot["started_at"]
    if period != "all":
        start = max(start, now - HOURS[period] * 3600)
    # Retain realized results before the selected display range as its opening value.
    pnl = Decimal(0)
    prior = Decimal(0)
    points = []
    estimated = 0
    for row in rows:
        pnl += Decimal(str(row["net"]))
        estimated += row["exact"] != 1
        if row["closed_at"] < start:
            prior = pnl
        else:
            points.append({"at": _iso(row["closed_at"]), "balance": str(budget + pnl)})
    closes = len(points)
    points.insert(0, {"at": _iso(start), "balance": str(budget + prior)})
    points.append({"at": _iso(now), "balance": str(budget + pnl)})
    return {"available": True, "period": period, "from": _iso(start), "to": _iso(now),
            "starting_budget": str(budget), "budget_source": pilot["budget_source"],
            "balance": str(budget + pnl), "pilot_pnl": str(pnl), "range_pnl": str(pnl - prior),
            "closed_in_range": closes, "estimated_fees_count": estimated, "open_trade": open_trade, "points": points}
