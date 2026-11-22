"""Read-only Up/Down paper account, trades, calibration stats and Discord event feed.

``make_router`` serves either desk: crypto at ``/api/updown`` and gold at ``/api/gold``.
"""
from functools import lru_cache
import json

from fastapi import APIRouter, Query

from .store import Store, data_directory
from .strategy import SETTINGS


@lru_cache(maxsize=4)
def _store(path):
    return Store(path)


def make_router(prefix, directory_fn, settings, tag):
    router = APIRouter(prefix=prefix, tags=[tag])

    def store():
        return _store(directory_fn() / "paper.sqlite3")

    @router.get("/account")
    def account():
        result = store().account()
        path = directory_fn() / "health.json"
        result["worker"] = json.loads(path.read_text()) if path.is_file() else None
        s = settings
        result["risk_settings"] = {"markets": [p.key for p in s.products], "min_edge": s.min_edge,
                                   "price_range": [s.price_min, s.price_max], "kelly_fraction": s.kelly_fraction,
                                   "max_stake_pct": s.max_stake, "max_open_stake_pct": s.max_open_stake,
                                   "daily_loss_limit": s.daily_loss_limit}
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

    return router


router = make_router("/api/updown", data_directory, SETTINGS, "Polymarket Up/Down paper trading")
