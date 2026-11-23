"""Read-only tracker API: status of every paper bot and one feed of their trades."""
from fastapi import APIRouter, Query

from . import collect, roster

router = APIRouter(prefix="/api/tracker", tags=["Tracker (all paper bots)"])


@router.get("/status")
def status():
    return collect()[0]


@router.get("/trades")
def trades(limit: int = Query(30, ge=1, le=200), desk: str | None = None):
    rows = collect()[1]
    if desk:
        rows = [t for t in rows if t["desk"] == desk]
    return {"trades": rows[:limit]}


@router.get("/roster")
def full_roster(desk: str | None = None, limit: int = Query(500, ge=1, le=2000)):
    """Every trade by every bot, grouped by bot, with totals."""
    return roster(limit=limit, desk=desk)
