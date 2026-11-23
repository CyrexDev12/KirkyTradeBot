"""Raw scoreboard data and the robust points series used for inspection."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from .data import TEAMS, canonical, schedule_rows, schedules
from .monte_carlo import completed_inputs, date
from .robust import point_history

router = APIRouter(prefix="/api/graphs", tags=["Model data"])


def points_series(rows, team: str, season: int, as_of: datetime) -> list[dict]:
    games = []
    for game in completed_inputs(rows, season):
        if team not in (game["home"], game["away"]) or date(game["released_at"]) > as_of:
            continue
        own, other = ("home", "away") if game["home"] == team else ("away", "home")
        games.append({"week": game["week"], "kickoff": game["kickoff"], "opponent": game[other],
                      "points_for": game[f"{own}_score"], "points_against": game[f"{other}_score"]})

    if games:
        scoring = point_history([game["points_for"] for game in games])
        allowed = point_history([game["points_against"] for game in games])
        for game, pf, pa in zip(games, scoring, allowed):
            game.update(robust_points_for=float(pf), robust_points_against=float(pa))
    return games


@router.get("/team-points")
def team_points(team: str = Query(min_length=2, max_length=3), season: int | None = Query(None, ge=1999, le=2100)):
    abbreviation = canonical(team.strip())
    if abbreviation not in TEAMS:
        raise HTTPException(404, "Use an NFL team abbreviation, such as KC or BUF.")
    try:
        rows = schedule_rows(schedules())
    except Exception as exc:
        raise HTTPException(503, "NFL schedule data is unavailable.") from exc

    now = datetime.now(timezone.utc)
    if season is None:
        season = max((row["season"] for row in rows if row["season"] <= now.year), default=now.year)
    games = points_series(rows, abbreviation, season, now)
    if not games:
        raise HTTPException(404, f"No eligible regular-season results for {abbreviation} in {season}.")
    return {"team": abbreviation, "season": season, "games": games,
            "method": "Results available at the time of this request. Adjusted values use the Monte Carlo team's "
                      "robust points filter on the displayed season. Actual scores are preserved."}
