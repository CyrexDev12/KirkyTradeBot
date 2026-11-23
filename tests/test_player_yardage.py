"""Player props must use pregame inputs and correctly verified contract terms."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import player_api
from backend.player_markets import evaluate_book, parse_props
from backend.player_yardage import Player, Projection, YardageData, is_pregame, kickoff
from backend.trading.book import BookSnapshot, OPEN


def game(game_id, week, at, *, finished=False, confirmed=True, status=None):
    return {"game_id": game_id, "season": at.year, "week": week, "kickoff": at.isoformat(),
            "away": "BUF", "home": "MIA", "home_score": 20 if finished else None,
            "away_score": 23 if finished else None, "kickoff_confirmed": confirmed, "source_status": status}


def test_player_outlier_guard_preserves_raw_observation_and_excludes_target_results():
    start = datetime(2026, 9, 4, 17, tzinfo=timezone.utc)
    matches = [game(str(week), week, start + timedelta(days=7 * week), finished=True) for week in range(1, 9)]
    observed_yards = [48, 49, 50, 51, 52, 310, 700, 800]
    rows = [
        {"player_id": "P", "game_id": str(week), "team": "BUF", "opponent_team": "MIA",
         "position": "RB", "rushing_yards": yards, "carries": 12, "passing_yards": 0, "attempts": 0}
        for week, yards in enumerate(observed_yards, 1)
    ]
    team_rows = [{"game_id": row["game_id"], "team": "BUF", "opponent_team": "MIA",
                  "rushing_yards": 120, "passing_yards": 215} for row in rows]
    data = YardageData(2026, matches, rows, team_rows, [])
    data.residuals = {("rushing", "back"): np.linspace(-2, 2, 100)}
    player = Player("P", "Test Runner", "BUF", "RB", 7)
    result = data.project(player, "rushing", matches[6], kickoff(matches[6])).payload

    assert result["games_sampled"] == 6
    assert result["interval_low"] < result["projection"] < result["interval_high"]
    assert result["interval_coverage"] == 0.8
    assert result["projection"] < 65
    assert result["season_average"] > 90
    assert result["robust_season_average"] < 60
    flagged = [sample for sample in result["history"] if sample["outlier"]]
    assert [(sample["week"], sample["yards"]) for sample in flagged] == [(6, 310)]
    assert flagged[0]["robust_yards"] < 80


def test_projection_defaults_to_next_player_match_and_labels_reconstructions(monkeypatch):
    now = datetime.now(timezone.utc)
    previous = game("started", 10, now - timedelta(hours=2))
    future = game("next", 11, now + timedelta(days=1))
    later = game("later", 12, now + timedelta(days=8))
    player = Player("00-123", "Test Runner", "BUF", "RB", 10)
    # Use one season even when the test runs near January.
    calendar = [{**match, "season": now.year} for match in (previous, future, later)]

    class Data:
        def active_players(self, teams, week):
            return [player]

        def project(self, athlete, stat, match, as_of):
            return Projection({"player": athlete.name, "game_id": match["game_id"], "stat": stat},
                              np.array([30, 40]), 10)

    monkeypatch.setattr(player_api, "_calendar", lambda: calendar)
    monkeypatch.setattr(player_api, "_source", lambda *_: Data())

    app = FastAPI()
    app.include_router(player_api.router)
    client = TestClient(app)
    query = {"player": "Test Runner", "stat": "rushing"}
    default = client.get("/api/players/projection", params=query)
    assert default.is_success, default.text
    assert default.json()["game_id"] == "next"
    historical = client.get("/api/players/projection", params={**query, "week": 10})
    assert historical.is_success and historical.json()["kind"] == "historical_reconstruction"
    unsupported = client.get("/api/players/projection", params={**query, "stat": "passing"})
    assert unsupported.status_code == 404


def prop_event(matchup):
    sides = [{"description": "Yes", "long": True, "teamId": 1, "tradable": True},
             {"description": "No", "long": False, "teamId": 1, "tradable": True}]
    return {
        "startTime": matchup["kickoff"],
        "teams": [{"id": 1, "abbreviation": "BUF"}, {"id": 2, "abbreviation": "MIA"}],
        "markets": [{
            "marketType": "props", "active": True, "status": "MARKET_STATUS_OPEN",
            "sportsMarketType": "football_player_passing_yards", "gameStartTime": matchup["kickoff"],
            "slug": "player-passing-125", "line": 125, "feeCoefficient": 0.095, "marketSides": sides,
            "metadata": {"playerName": "Test Passer", "teamId": 1, "lineLabel": "125+"},
            "description": ("This market will settle to Yes if Test Passer records 125 or more passing yards. "
                            "Overtime is included if played. The listed player must participate by taking at least one snap; "
                            "otherwise, the market will settle to the last fair market price. "
                            "The market will settle based on the official box score at the conclusion of the game."),
        }],
    }


def test_inclusive_yards_market_and_fee_adjusted_book_price():
    now = datetime.now(timezone.utc)
    future = game("future", 11, now + timedelta(hours=2))
    event = prop_event(future)
    prop, = parse_props(future, event)
    assert (prop.label, prop.line, prop.threshold) == ("125+", 124.5, 125)

    # Integer 125 wins the Yes contract; integer 124 does not.
    projection = Projection({"player_id": "P", "stat": "passing"}, np.array([124, 125, 126]), 29)
    assert projection.over_probability(prop.line) == 0.625
    book = BookSnapshot(prop.slug, now, now, OPEN, ((Decimal("0.41"), Decimal("20")),),
                        ((Decimal("0.45"), Decimal("5")), (Decimal("0.48"), Decimal("7"))))
    pick = evaluate_book(prop, projection, 0.625, book, now)
    assert pick["side"] == "over" and pick["price"] == 0.465 and pick["quantity"] == 10
    assert pick["edge"] < 0.625 - 0.465
    assert "last fair market price" in pick["settlement"]

    wrong = deepcopy(event)
    wrong["markets"][0]["description"] = wrong["markets"][0]["description"].replace("125 or more", "more than 125")
    assert not parse_props(future, wrong)
    wrong = deepcopy(event)
    wrong["markets"][0]["description"] = wrong["markets"][0]["description"].replace("last fair market price", "No")
    assert not parse_props(future, wrong)
    wrong = deepcopy(event)
    wrong["markets"][0]["sportsMarketType"] = "football_player_receiving_yards"
    assert not parse_props(future, wrong)
    assert not is_pregame({**future, "kickoff_confirmed": False}, now)
    assert not is_pregame({**future, "source_status": "Canceled"}, now)
    assert not is_pregame({**future, "source_status": "Live"}, now)
