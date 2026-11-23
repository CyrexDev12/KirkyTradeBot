"""API for individual player forecasts and verified weekly props."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from .data import canonical, schedule_rows, schedules, TEAMS
from .player_markets import scan_week
from .player_yardage import is_pregame, kickoff, name_key, scheduled, valid_stat, yardage_data

router = APIRouter(prefix="/api/players", tags=["NFL player yardage"])
_LOG = logging.getLogger(__name__)


def _calendar():
    try:
        return schedule_rows(schedules())
    except Exception as exc:
        _LOG.warning("NFL schedule unavailable for player yardage: %s", exc)
        raise HTTPException(503, "NFL schedule unavailable.") from exc


def _season(calendar, season, now):
    if season is None:
        possible = [game["season"] for game in calendar if game["season"] <= now.year]
        season = max(possible, default=None)
    games = [game for game in calendar if game["season"] == season]
    if not games or season < 2015:
        raise HTTPException(404, "NFL season unavailable for player-yardage forecasts.")
    return season, games


def _week(calendar, season, week, now):
    season, games = _season(calendar, season, now)
    if week is None:
        week = next((game["week"] for game in games if is_pregame(game, now)), games[-1]["week"])
    selected = [game for game in games if game["week"] == week]
    if not selected:
        raise HTTPException(404, "NFL schedule has no games for that week.")
    return season, week, selected


def _source(season, calendar):
    try:
        return yardage_data(season, calendar)
    except Exception as exc:
        _LOG.warning("NFLverse player-yardage data unavailable: %s", exc)
        raise HTTPException(503, "NFLverse player stats or weekly rosters are temporarily unavailable.") from exc


@router.get("/projection")
def projection(player: str = Query(min_length=2, max_length=100),
               stat: Literal["rushing", "passing"] = "rushing",
               team: str | None = Query(None, min_length=2, max_length=4),
               season: int | None = Query(None, ge=2015),
               week: int | None = Query(None, ge=1, le=22)):
    """Look up a QB or RB by full name or NFLverse GSIS ID."""
    team = canonical(team)
    if team is not None and team not in TEAMS:
        raise HTTPException(422, "Use a valid NFL team abbreviation.")
    now = datetime.now(timezone.utc)
    calendar = _calendar()
    season, games = _season(calendar, season, now)
    if week is None:
        games = [game for game in games if is_pregame(game, now)]
    else:
        games = [game for game in games if game["week"] == week and scheduled(game)]
    if not games:
        raise HTTPException(404, "NFL schedule has no confirmed games for that selection.")
    data = _source(season, calendar)
    query = name_key(player)
    candidates = []
    for game in games:
        lineup = data.active_players((game["home"], game["away"]), game["week"])
        for athlete in lineup:
            if team and athlete.team != team:
                continue
            if query and (name_key(athlete.name) == query or athlete.id == player.strip()):
                candidates.append((athlete, game))
    candidates = [(athlete, game) for athlete, game in candidates if valid_stat(athlete.position, stat)]
    if not candidates:
        raise HTTPException(404, "No active player matched this name, stat, and week. Try a full name or a different week.")
    first_kickoff = min(kickoff(game) for _, game in candidates)
    first_candidates = [(athlete, game) for athlete, game in candidates if kickoff(game) == first_kickoff]
    if len(first_candidates) > 1 or len({athlete.id for athlete, _ in candidates}) > 1:
        raise HTTPException(409, "More than one player or team matches. Specify the team abbreviation or GSIS ID.")
    athlete, game = first_candidates[0]
    result = data.project(athlete, stat, game, now)
    if result is None:
        raise HTTPException(404, "Not enough completed player or opponent history for an 80% prediction interval.")
    return {**result.payload, "kind": "pregame" if is_pregame(game, now) else "historical_reconstruction"}


@router.get("/picks")
async def picks(season: int | None = Query(None, ge=2015),
                week: int | None = Query(None, ge=1, le=22)):
    """Return up to 10 distinct player and stat combinations with verified quotes."""
    calendar = await asyncio.to_thread(_calendar)
    now = datetime.now(timezone.utc)
    season, week, games = _week(calendar, season, week, now)
    data = await asyncio.to_thread(_source, season, calendar) if any(is_pregame(game, now) for game in games) else None
    return await scan_week(games, data, season, week)
