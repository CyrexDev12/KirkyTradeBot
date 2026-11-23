"""Team charts expose the raw scores and the robust model inputs together."""
from datetime import datetime, timedelta, timezone

from backend.team_graph import points_series


def test_team_chart_keeps_outlier_actuals_and_excludes_unavailable_results():
    start = datetime(2025, 9, 7, 17, tzinfo=timezone.utc)
    rows = []
    for week, score in enumerate([20] * 6 + [60, 100], 1):
        kickoff = start + timedelta(weeks=week - 1)
        rows.append({"season": 2025, "week": week, "game_type": "REG", "game_id": f"game-{week}",
                     "kickoff": kickoff.isoformat(), "home": "KC", "away": "BUF",
                     "neutral_site": False, "home_score": score, "away_score": 19,
                     "result": score - 19, "result_available_at": (kickoff + timedelta(hours=4)).isoformat()})

    series = points_series(rows, "KC", 2025, start + timedelta(weeks=7))
    assert len(series) == 7
    assert series[-1]["points_for"] == 60
    assert series[-1]["robust_points_for"] < 60
    assert all(game["points_against"] == game["robust_points_against"] == 19 for game in series)
