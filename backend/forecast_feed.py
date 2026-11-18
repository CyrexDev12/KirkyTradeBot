"""Sports-only feed shared by the Discord bot and API callers."""
from datetime import datetime, timezone
import json
import os

from .data import DATA, schedule_rows, schedules, team_info
from .mc_archive import get_forecasts
from .model import PREDICTIONS
from .totals import PREDICTIONS as TOTAL_PREDICTIONS


def read_predictions(path):
    if not path.exists():
        return {}, None
    return json.loads(path.read_text()), datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def schedule_status(now):
    path = DATA / "schedules.parquet"
    updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) if path.exists() else None
    stale_minutes = max(30, 2 * float(os.getenv("NFL_SCHEDULE_CACHE_MINUTES", "360")))
    return {"schedule_updated_at": updated.isoformat() if updated else None,
            "schedule_stale": updated is None or (now - updated).total_seconds() > stale_minutes * 60}


def upcoming(hours=48):
    rows = schedule_rows(schedules())
    now = datetime.now(timezone.utc)
    selected = [g for g in rows if g.get("kickoff_confirmed") and
                0 < (datetime.fromisoformat(g["kickoff"]) - now).total_seconds() <= hours*3600 and
                g["home_score"] is None and g["away_score"] is None and
                str(g.get("source_status") or "").lower() not in {"cancelled", "canceled", "postponed", "suspended", "live"}]
    return {"games": selected, **schedule_status(now)}


def game_forecast(game):
    predictions, refreshed = read_predictions(PREDICTIONS)
    totals, totals_refreshed = read_predictions(TOTAL_PREDICTIONS)
    now = datetime.now(timezone.utc)
    completed = game["home_score"] is not None and game["away_score"] is not None
    status = "Final" if completed else "Upcoming" if datetime.fromisoformat(game["kickoff"]) > now else "Live"
    return {
        "game": {**game, "home_team": team_info(game["home"]), "away_team": team_info(game["away"]), "status": status},
        "prediction": predictions.get(game["game_id"]),
        "total_prediction": totals.get(game["game_id"]),
        "monte_carlo": get_forecasts(game["season"], [game])[game["game_id"]],
        "existing_refreshed_at": refreshed,
        "totals_refreshed_at": totals_refreshed,
        **schedule_status(now),
    }
