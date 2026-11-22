"""Read-only Up/Down paper account, trades, calibration stats and Discord event feed."""
from functools import lru_cache
import json

from fastapi import APIRouter, Query

from .store import Store, data_directory
from .strategy import SETTINGS

router = APIRouter(prefix="/api/updown", tags=["Polymarket Up/Down paper trading"])


@lru_cache(maxsize=1)
def _store(path):
    return Store(path)


def store():
    return _store(data_directory() / "paper.sqlite3")


@router.get("/account")
def account():
    result = store().account()
    path = data_directory() / "health.json"
    result["worker"] = json.loads(path.read_text()) if path.is_file() else None
    s = SETTINGS
    result["risk_settings"] = {"assets": list(s.assets), "min_edge": s.min_edge, "price_range": [s.price_min, s.price_max],
                               "kelly_fraction": s.kelly_fraction, "max_stake_pct": s.max_stake,
                               "max_open_stake_pct": s.max_open_stake, "daily_loss_limit": s.daily_loss_limit}
    return result


@router.get("/trades")
def trades(limit: int = Query(10, ge=1, le=100)):
    return {"trades": store().trades(limit)}


@router.get("/stats")
def stats():
    return store().stats()


@router.get("/events")
def events(after: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    return store().events(after, limit)
