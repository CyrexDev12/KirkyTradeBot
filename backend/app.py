"""Read-only NFL fair value API and static dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .data import DATA, FEATURE_PATH, MODEL_VERSION, schedule_rows, schedules, team_info
from .markets import get_week_markets, get_week_totals, valuation
from .mc_archive import get_forecasts
from .mc_validation import metrics as mc_metrics
from .mc_value import market_values
from .forecast_feed import game_forecast, upcoming
from .model import get_metrics, get_predictions, needs_refresh as winner_needs_refresh, train
from .trading.api import router as paper_router
from .crypto.api import router as crypto_router
from .updown.api import router as updown_router
from .gold.api import router as gold_router
from .player_api import router as player_router
from .team_graph import router as team_graph_router
from .totals import (
    VERSION as TOTAL_VERSION,
    get_metrics as get_total_metrics,
    get_predictions as get_total_predictions,
    needs_refresh as totals_needs_refresh,
    over_probability,
    train as train_totals,
)

app = FastAPI(title="NFL Fair Value", version="1.0.0")
STATIC = Path(__file__).resolve().parents[1] / "frontend"
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
app.include_router(paper_router)
app.include_router(crypto_router)
app.include_router(updown_router)
app.include_router(gold_router)
app.include_router(player_router)
app.include_router(team_graph_router)
logger = logging.getLogger(__name__)
_refresh_task = None
_mc_task = None


def _prepare_mc():
    rows = _all_games()
    now = datetime.now(timezone.utc)
    upcoming = [g for g in rows if g["game_type"] == "REG" and datetime.fromisoformat(g["kickoff"]) > now]
    if upcoming:
        season = upcoming[0]["season"]
        get_forecasts(season, [g for g in upcoming if g["season"] == season])


async def _refresh_mc_loop():
    while True:
        try:
            await asyncio.to_thread(_prepare_mc)
        except Exception:
            logger.exception("Monte Carlo refresh failed")
        await asyncio.sleep(30*60)


async def _refresh_model_loop():
    while True:
        try:
            if winner_needs_refresh():
                await asyncio.to_thread(train)
            if totals_needs_refresh():
                await asyncio.to_thread(train_totals)
        except Exception:
            logger.exception("NFL model refresh failed; existing artifacts remain available")
        await asyncio.sleep(30*60)


@app.on_event("startup")
async def _start_refresh():
    global _refresh_task, _mc_task
    _refresh_task = asyncio.create_task(_refresh_model_loop())
    _mc_task = asyncio.create_task(_refresh_mc_loop())


@app.on_event("shutdown")
async def _stop_refresh():
    if _refresh_task:
        _refresh_task.cancel()
    if _mc_task:
        _mc_task.cancel()


def _all_games() -> list[dict]:
    try:
        return schedule_rows(schedules())
    except Exception as exc:
        raise HTTPException(503, f"NFLverse schedule unavailable: {exc}") from exc


def _selection(season: int | None, week: int | None) -> tuple[int, int, list[dict]]:
    rows = _all_games()
    now = datetime.now(timezone.utc)
    if not rows:
        raise HTTPException(503, "No NFL schedule data")
    if season is None:
        season = max(r["season"] for r in rows if r["season"] <= now.year)
    sr = [r for r in rows if r["season"] == season]
    if not sr:
        raise HTTPException(404, "Season not available")
    if week is None:
        weeks = sorted({r["week"] for r in sr})
        active = [w for w in weeks if min(datetime.fromisoformat(r["kickoff"]) for r in sr if r["week"] == w) <= now
                  and any(datetime.fromisoformat(r["kickoff"]) > now for r in sr if r["week"] == w)]
        week = active[-1] if active else next((w for w in weeks if any(datetime.fromisoformat(r["kickoff"]) > now for r in sr if r["week"] == w)), weeks[-1])
    games = [r for r in sr if r["week"] == week]
    if not games:
        raise HTTPException(404, "Week not available")
    return season, week, games


def _status(game: dict) -> str:
    if game["home_score"] is not None and game["away_score"] is not None:
        return "Final"
    return "Upcoming" if datetime.fromisoformat(game["kickoff"]) > datetime.now(timezone.utc) else "Live"


def _base(game: dict) -> dict:
    return {**game, "home_team": team_info(game["home"]), "away_team": team_info(game["away"]), "status": _status(game)}


def _predict_data() -> tuple[dict, str | None]:
    try:
        return get_predictions(), None
    except RuntimeError as exc:
        return {}, str(exc)


def _total_predict_data() -> tuple[dict, str | None]:
    try:
        return get_total_predictions(), None
    except RuntimeError as exc:
        return {}, str(exc)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/api/upcoming")
def upcoming_games(hours: int = Query(48, ge=1, le=168)):
    return upcoming(hours)


@app.get("/api/games/{game_id}/forecast")
def forecast(game_id: str):
    game = next((g for g in _all_games() if g["game_id"] == game_id), None)
    if game is None:
        raise HTTPException(404, "Game not found")
    return game_forecast(game)


@app.get("/api/meta")
def meta():
    rows = _all_games()
    seasons = sorted({r["season"] for r in rows}, reverse=True)
    weeks = {str(s): sorted({r["week"] for r in rows if r["season"] == s}) for s in seasons}
    season, week, _ = _selection(None, None)
    status = json.loads((DATA / "data_status.json").read_text()) if (DATA / "data_status.json").exists() else None
    return {"seasons": seasons, "weeks": weeks, "default_season": season, "default_week": week,
            "model_version": MODEL_VERSION, "total_model_version": TOTAL_VERSION, "data_status": status}


@app.get("/api/games")
def games(season: int | None = None, week: int | None = None):
    season, week, selected = _selection(season, week)
    return {"season": season, "week": week, "games": [_base(g) for g in selected]}


@app.get("/api/predictions")
def predictions(season: int | None = None, week: int | None = None):
    season, week, selected = _selection(season, week)
    preds, error = _predict_data()
    return {"season": season, "week": week, "predictions": {g["game_id"]: preds.get(g["game_id"]) for g in selected}, "error": error}


@app.get("/api/total-predictions")
def total_predictions(season: int | None = None, week: int | None = None):
    season, week, selected = _selection(season, week)
    preds, error = _total_predict_data()
    return {"season": season, "week": week, "predictions": {g["game_id"]: preds.get(g["game_id"]) for g in selected}, "error": error}


@app.get("/api/markets")
async def markets(season: int | None = None, week: int | None = None, quantity: float = Query(100, gt=0, le=10000), refresh: bool = False):
    season, week, selected = _selection(season, week)
    return {"season": season, "week": week, **await get_week_markets(selected, quantity, refresh)}


@app.get("/api/monte-carlo/predictions")
def monte_carlo_predictions(season: int, week: int):
    season, week, selected = _selection(season, week)
    return {"season": season, "week": week, "predictions": get_forecasts(season, selected)}


@app.get("/api/games/{game_id}/monte-carlo")
def monte_carlo_game(game_id: str):
    game = next((g for g in _all_games() if g["game_id"] == game_id), None)
    if not game:
        raise HTTPException(404, "Game not found")
    return get_forecasts(game["season"], [game])[game_id]


@app.get("/api/model/monte-carlo-metrics")
def monte_carlo_metrics(season: int):
    return mc_metrics(season)


@app.get("/api/totals")
async def totals(season: int | None = None, week: int | None = None, quantity: float = Query(100, gt=0, le=10000), refresh: bool = False):
    season, week, selected = _selection(season, week)
    return {"season": season, "week": week, **await get_week_totals(selected, quantity, refresh)}


def _total_value(total_prediction: dict | None, totals: dict | None, quantity: float) -> dict | None:
    if not totals:
        return None
    result = {**totals, "lines": []}
    estimate = (total_prediction or {}).get("predicted_total")
    for quote in totals.get("lines") or []:
        over = over_probability(estimate, quote["line"])
        entry = {**quote, "model_over": over, "model_under": 1-over if over is not None else None,
                 "over_value": valuation(over, quote["outcomes"].get("over"), quantity),
                 "under_value": valuation(1-over if over is not None else None, quote["outcomes"].get("under"), quantity)}
        result["lines"].append(entry)
    result["featured"] = next((q for q in result["lines"] if q["line"] == totals.get("featured_line")), None)
    return result


@app.get("/api/value")
async def value(season: int | None = None, week: int | None = None, quantity: float = Query(100, gt=0, le=10000), refresh: bool = False):
    season, week, selected = _selection(season, week)
    return await _values(season, week, selected, quantity, refresh, True)


async def _values(season, week, selected, quantity, refresh, include_existing):
    preds, error = _predict_data() if include_existing else ({}, None)
    total_preds, total_error = _total_predict_data() if include_existing else ({}, None)
    market_data = await get_week_markets(selected, quantity, refresh)
    total_data = await get_week_totals(selected, quantity, refresh)
    result = {}
    for game in selected:
        gid = game["game_id"]
        prediction = preds.get(gid)
        market = market_data["markets"].get(gid)
        home_p = prediction["home_probability"] if prediction else None
        result[gid] = {"prediction": prediction, "total_prediction": total_preds.get(gid), "market": market,
                       "totals": _total_value(total_preds.get(gid), total_data["totals"].get(gid), quantity),
                       "home_value": valuation(home_p, (market or {}).get("outcomes", {}).get(game["home"]), quantity),
                       "away_value": valuation(1-home_p if home_p is not None else None, (market or {}).get("outcomes", {}).get(game["away"]), quantity)}
    return {"season": season, "week": week, "values": result, "market_fetched_at": market_data["fetched_at"],
            "market_error": market_data["error"], "total_market_error": total_data["error"],
            "model_error": error, "total_model_error": total_error}


@app.get("/api/dashboard")
async def dashboard(season: int | None = None, week: int | None = None, quantity: float = Query(100, gt=0, le=10000),
                    refresh: bool = False, model: Literal["existing", "monte_carlo", "compare"] = "existing"):
    season, week, selected = _selection(season, week)
    forecasts = await asyncio.to_thread(get_forecasts, season, selected) if model != "existing" else {}
    values = await _values(season, week, selected, quantity, refresh, model != "monte_carlo")
    games = [{**_base(g), **values["values"][g["game_id"]]} for g in selected]
    for g in games:
        if g["game_id"] in forecasts:
            f = forecasts[g["game_id"]]
            upcoming = g["status"] == "Upcoming"
            g.update(monte_carlo=f, mc_values=market_values(f, g["market"] if upcoming else None,
                                                           g["totals"] if upcoming else None, quantity))
    return {"season": season, "week": week, "games": games,
            "market_fetched_at": values["market_fetched_at"], "market_error": values["market_error"],
            "total_market_error": values["total_market_error"], "model_error": values["model_error"],
            "total_model_error": values["total_model_error"]}


@app.get("/api/games/{game_id}/analysis")
async def analysis(game_id: str, quantity: float = Query(100, gt=0, le=10000)):
    games = _all_games()
    game = next((g for g in games if g["game_id"] == game_id), None)
    if not game:
        raise HTTPException(404, "Game not found")
    feat_path = FEATURE_PATH
    features = None
    if feat_path.exists():
        features = next((r for r in json.loads(feat_path.read_text()) if r["game_id"] == game_id), None)
    preds, error = _predict_data()
    total_preds, total_error = _total_predict_data()
    market_data = await get_week_markets([game], quantity)
    total_data = await get_week_totals([game], quantity)
    market = market_data["markets"].get(game_id)
    prediction = preds.get(game_id)
    home_p = prediction["home_probability"] if prediction else None
    return {"game": _base(game), "prediction": prediction, "total_prediction": total_preds.get(game_id),
            "totals": _total_value(total_preds.get(game_id), total_data["totals"].get(game_id), quantity), "market": market,
            "home_value": valuation(home_p, (market or {}).get("outcomes", {}).get(game["home"]), quantity),
            "away_value": valuation(1-home_p if home_p is not None else None, (market or {}).get("outcomes", {}).get(game["away"]), quantity),
            "home_metrics": features.get("home_metrics") if features else None,
            "away_metrics": features.get("away_metrics") if features else None,
            "model_features": features.get("features") if features else None,
            "model_version": MODEL_VERSION, "total_model_version": TOTAL_VERSION, "model_error": error,
            "total_model_error": total_error, "market_error": market_data["error"], "total_market_error": total_data["error"],
            "explanation": "Pregame team efficiency, recent form, opponent strength, rest, and Elo differences feed a calibrated gradient-boosted model. Feature values are descriptive, not causal attributions."}


@app.get("/api/model/metrics")
def model_metrics():
    try:
        return get_metrics()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/model/totals-metrics")
def total_model_metrics():
    try:
        return get_total_metrics()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
