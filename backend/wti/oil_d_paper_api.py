"""Private read-only views of the Oil D execution paper account."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import time

from fastapi import APIRouter, Header, Query

from ..trading.api import operator
from . import oil_d_paper_store
from .commodity import WTI
from .live_chart import ChartTime, HOURS
from .live_timing import as_decimal, readonly


router = APIRouter(prefix="/api/wti/paper-execution", tags=["Oil D execution paper operator"])
STATUS_MAX_AGE = 30


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def _path() -> Path:
    return oil_d_paper_store.paper_path(WTI.directory())


def status(path: Path, now: float) -> dict:
    data = oil_d_paper_store.report(path, now - 24 * 3600, now)
    heartbeat = data.get("last_heartbeat")
    age = now - datetime.fromisoformat(heartbeat).timestamp() if heartbeat else None
    current = age is not None and -2 <= age < STATUS_MAX_AGE
    available = data["started_at"] is not None
    if heartbeat is None:
        state, message = "waiting", "Waiting for the Oil D execution paper worker."
    elif not current:
        state, message = "offline", "The Oil D execution paper worker has not checked in recently."
    else:
        state = data.get("worker_state") or "waiting"
        message = data.get("worker_reason")
    return {**data, "mode": "PAPER_ONLY", "available": available, "state": state, "message": message,
            "health_current": current, "entries_allowed": current and bool(data.get("entries_allowed")),
            "latest_id": data.get("latest_id", 0), "equity": str(data["balance"]),
            **{key: str(value) for key, value in data.items() if isinstance(value, Decimal)}}


def chart(path: Path, period: ChartTime, now: float) -> dict:
    if not path.is_file():
        return {"available": False, "message": "Waiting for the Oil D execution paper journal."}
    with readonly(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE name IN ('account','trades')")}
        if tables != {"account", "trades"}:
            return {"available": False, "message": "Waiting for the Oil D execution paper journal."}
        account = db.execute("SELECT started_at FROM account WHERE id=1").fetchone()
        if account is None or account["started_at"] > now:
            return {"available": False, "message": "Waiting for the Oil D execution paper account to start."}
        start = account["started_at"]
        rows = db.execute("""SELECT closed_at,json_extract(payload,'$.net_pnl') AS net
                             FROM trades WHERE state='closed' AND closed_at BETWEEN ? AND ?
                               AND json_extract(payload,'$.net_pnl') IS NOT NULL
                             ORDER BY closed_at,slug""", (start, now)).fetchall()
        open_trade = db.execute("""SELECT 1 FROM trades WHERE state IN ('open','settling')
                                   AND sent_at<=? LIMIT 1""", (now,)).fetchone() is not None

    if period != "all":
        start = max(start, now - HOURS[period] * 3600)
    budget = oil_d_paper_store.STARTING_BUDGET
    prior = realized = Decimal(0)
    points = []
    valid_closes = 0
    for row in rows:
        net = as_decimal(row["net"])
        if net is None:
            continue
        valid_closes += 1
        realized += net
        if row["closed_at"] < start:
            prior = realized
        else:
            points.append({"at": _iso(row["closed_at"]), "balance": str(budget + realized)})
    closes = len(points)
    points.insert(0, {"at": _iso(start), "balance": str(budget + prior)})
    points.append({"at": _iso(now), "balance": str(budget + realized)})
    return {"mode": "PAPER_ONLY", "available": True, "period": period, "from": _iso(start), "to": _iso(now),
            "starting_budget": str(budget), "budget_source": "paper_account", "balance": str(budget + realized),
            "pilot_pnl": str(realized), "range_pnl": str(realized - prior), "closed_in_range": closes,
            "estimated_fees_count": valid_closes, "open_trade": open_trade, "points": points}


@router.get("/status")
def oil_d_paper_status(x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    return status(_path(), time.time())


@router.get("/events")
def oil_d_paper_events(after: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                       x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    return oil_d_paper_store.events(_path(), after=after, limit=limit)


@router.get("/chart")
def oil_d_paper_chart(time: ChartTime = "24h", x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    return chart(_path(), time, datetime.now(timezone.utc).timestamp())
