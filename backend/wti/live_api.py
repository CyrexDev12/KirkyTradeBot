"""Private operator endpoints for the Oil D pilot. Trading keys stay in its worker."""
from datetime import datetime, timezone
from functools import lru_cache
import time

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field

from ..trading.api import operator
from .commodity import WTI
from .live_chart import ChartTime, chart
from .live_store import LiveStore, live_path

router = APIRouter(prefix="/api/wti/live", tags=["Oil D live operator"])


@lru_cache(maxsize=4)
def _store(path):
    return LiveStore(path)


def store():
    return _store(live_path(WTI.directory()))


def report(journal):
    data = journal.status()
    configured = bool(data.get("worker_configured"))
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["last_heartbeat"])).total_seconds() \
        if data["last_heartbeat"] else None
    data["configured"] = configured
    data["enabled"] = bool(configured and data.get("entries_configured") and not data["paused"]
                           and age is not None and age < 30)
    if age is None or age >= 30:
        data.update(state="offline", message="Waiting for a current Oil D live worker heartbeat.")
    elif not configured and not data["paused"]:
        data["state"] = "unconfigured"
    return data


@router.get("/status")
def live_status(x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    return report(store())


@router.get("/chart")
def live_chart(time: ChartTime = "24h", x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    return chart(live_path(WTI.directory()), time, datetime.now(timezone.utc).timestamp())


@router.get("/events")
def live_events(after: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    return store().events(after, limit)


class Pause(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


@router.post("/off")
def live_off(body: Pause, x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    journal = store()
    journal.disable(time.time(), f"New entries stopped by {body.actor}. Existing positions stay under management.")
    return report(journal)
