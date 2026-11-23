"""Read-only energy desk API: model leaderboard, per-model accounts and trades, news/info.

``make_router(commodity)`` serves one desk: WTI crude at ``/api/wti`` (this module's
``router``) and natural gas at ``/api/natgas`` (``backend.natgas.api``)."""
from datetime import datetime, timezone
from functools import lru_cache
import json
import time

from fastapi import APIRouter, HTTPException, Query

from ..updown.store import Store
from .commodity import WTI, Commodity
from .events import Calendar, front_month_expiry
from .models import MODEL_INFO


@lru_cache(maxsize=32)
def _store(path):
    return Store(path)


def make_router(c: Commodity) -> APIRouter:
    router = APIRouter(prefix=f"/api/{c.api}", tags=[f"Polymarket {c.name} Up/Down paper trading"])

    def store(model_id: str) -> Store:
        model_id = model_id.upper()
        if model_id not in c.models():
            raise HTTPException(404, f"Unknown model {model_id}; models: {', '.join(c.models())}")
        return _store(c.directory() / "models" / model_id / "paper.sqlite3")

    def health():
        path = c.directory() / "health.json"
        try:
            return json.loads(path.read_text()) if path.is_file() else None
        except ValueError:
            return None

    def model_row(model_id: str):
        s = store(model_id)
        acct, stats = s.account(), s.stats()
        closed = [t for t in s.trades(500) if t.get("status") == "CLOSED"]
        count = lambda reason: sum(t.get("exit_reason") == reason for t in closed)  # noqa: E731
        name, idea = MODEL_INFO[model_id]
        return {"model": model_id, "name": name, "idea": idea, "lead": model_id == c.lead(),
                "balance": acct["balance"], "realized_pnl": acct["realized_pnl"], "today_pnl": acct["today_pnl"],
                "open_count": len(acct["open_trades"]), "open_stake": acct["open_stake"],
                "bets": stats["closed_trades"], "wins": stats["wins"], "losses": stats["losses"],
                "outs": count("take_profit") + count("pre_news"), "targets": count("target"),
                "stops": count("stop_loss"), "staked": stats["staked"], "roi": stats["return_on_stake"],
                "brier_model": stats["model_brier"], "brier_market": stats["market_mid_brier"],
                "samples": stats["observations"]}

    @router.get("/models")
    def models():
        """Leaderboard: every model's paper account side by side, best balance first."""
        rows = sorted((model_row(m) for m in c.models()), key=lambda r: -r["balance"])
        s = c.settings()
        return {"mode": "PAPER_ONLY", "commodity": c.key, "name": c.name, "lead_model": c.lead(),
                "markets": [p.key for p in s.products], "models": rows, "worker": health(),
                "risk_settings": {"min_edge": s.min_edge, "price_range": [s.price_min, s.price_max],
                                  "kelly_fraction": s.kelly_fraction, "max_stake_pct": s.max_stake,
                                  "max_open_stake_pct": s.max_open_stake, "daily_loss_limit": s.daily_loss_limit,
                                  "take_profit": s.take_profit_target, "stop_loss": s.stop_loss}}

    @router.get("/info")
    def info(hours: int = Query(48, ge=1, le=168)):
        """Scheduled releases ahead, what the desk detected recently, and the front futures month."""
        now = time.time()
        calendar = Calendar(c.directory() / "events.json", c.recurring, c.jumps)
        label, expiry = front_month_expiry(datetime.fromtimestamp(now, timezone.utc).date(), c.expiry)
        recent = []
        path = c.directory() / "info.sqlite3"
        if path.is_file():
            from .worker import InfoLog
            recent = InfoLog(path).recent(now - 86400)
        return {"commodity": c.key, "upcoming": [e.to_dict() for e in calendar.between(now, now + hours * 3600)],
                "detected_last_24h": recent,
                "front_month": {"contract": label, "last_trading_day": expiry.isoformat(),
                                "note": "Chainlink switches to the next month on its own schedule; a price jump "
                                        "with no news around then is treated as a possible switch."},
                "worker": health()}

    @router.get("/info/events")
    def info_events(after: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        path = c.directory() / "info.sqlite3"
        if not path.is_file():
            return {"events": [], "latest_id": 0}
        from .worker import InfoLog
        return InfoLog(path).since(after, limit)

    @router.get("/{model_id}/account")
    def account(model_id: str):
        result = store(model_id).account()
        result.update(model=model_id.upper(), model_name=MODEL_INFO[model_id.upper()][0], worker=health())
        return result

    @router.get("/{model_id}/trades")
    def trades(model_id: str, limit: int = Query(10, ge=1, le=200)):
        return {"model": model_id.upper(), "trades": store(model_id).trades(limit)}

    @router.get("/{model_id}/stats")
    def stats(model_id: str):
        return {"model": model_id.upper(), **store(model_id).stats()}

    @router.get("/{model_id}/events")
    def events(model_id: str, after: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        return store(model_id).events(after, limit)

    return router


router = make_router(WTI)
