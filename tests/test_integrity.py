from copy import deepcopy
import json

import numpy as np
import polars as pl
import pytest

from backend.data import build_features
from backend.markets import execution, match_event, match_total_markets, valuation
from backend import model, totals


def test_current_game_play_by_play_cannot_enter_its_pregame_features():
    games = [
        {"game_id": "g1", "season": 2025, "week": 1, "kickoff": "2025-09-07T17:00:00+00:00", "home": "BUF", "away": "MIA", "home_score": 21, "away_score": 17, "home_rest": 7, "away_rest": 7},
        {"game_id": "g2", "season": 2025, "week": 2, "kickoff": "2025-09-14T17:00:00+00:00", "home": "MIA", "away": "BUF", "home_score": 10, "away_score": 24, "home_rest": 7, "away_rest": 7},
    ]
    base = pl.DataFrame([{"game_id": g, "team": t, "off_epa": value} for g,t,value in [("g1","BUF",0.12),("g1","MIA",-0.12),("g2","BUF",0.2),("g2","MIA",-0.2)]])
    changed = base.with_columns(pl.when(pl.col("game_id") == "g2").then(pl.lit(1000.0)).otherwise(pl.col("off_epa")).alias("off_epa"))
    assert build_features(games, base)[1]["features"] == build_features(games, changed)[1]["features"]
    assert build_features(games, base)[1]["total_features"] == build_features(games, changed)[1]["total_features"]
    altered_scores = deepcopy(games)
    altered_scores[1]["home_score"] = 99
    assert build_features(games, base)[1]["total_features"] == build_features(altered_scores, base)[1]["total_features"]


def test_isolated_score_and_efficiency_spikes_are_bounded_in_future_features():
    games, stats = [], []
    scores = [18, 20, 22, 23, 25, 20, 21, 180, 22]
    for i, score in enumerate(scores):
        games.append(dict(game_id=str(i), season=2025, week=i + 1, kickoff=f"2025-09-{i + 1:02d}T18:00:00+00:00",
                          home="BUF", away="MIA", home_score=score, away_score=21, home_rest=7, away_rest=7))
        stats.append(dict(game_id=str(i), team="BUF", off_epa=4.0 if i == 7 else .12))
    baseline_games = [*games[:7], {**games[7], "home_score": 70}, games[8]]
    baseline_stats = [*stats[:7], {**stats[7], "off_epa": .60}, stats[8]]
    baseline_rows = build_features(baseline_games, pl.DataFrame(baseline_stats))
    rows = build_features(games, pl.DataFrame(stats))
    assert rows[7]["home_score"] == 180
    assert rows[7]["features"] == baseline_rows[7]["features"]
    assert rows[8]["total_features"] == baseline_rows[8]["total_features"]
    assert rows[8]["features"] == baseline_rows[8]["features"]
    assert rows[8]["total_features"]["home_points_for"] < 25


@pytest.mark.parametrize("module, key, new_value", [(model, "home_probability", .61), (totals, "predicted_total", 42.0)])
def test_robust_model_upgrade_keeps_only_started_pregame_snapshots(module, key, new_value, tmp_path, monkeypatch):
    rows = [dict(game_id="played", kickoff="2001-09-09T18:00:00+00:00", home_score=30, away_score=25, season=2001),
            dict(game_id="upcoming", kickoff="2099-09-13T18:00:00+00:00", home_score=None, away_score=None, season=2099)]
    old = {gid: {"kind": "live_pregame", key: .8 if module is model else 64.0, "model_version": "legacy"}
           for gid in ("played", "upcoming")}
    for name in ("LEGACY_PREDICTIONS", "PREDICTIONS", "METRICS", "ARTIFACT", "RESIDUALS"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, tmp_path / f"{name}.json")
    module.LEGACY_PREDICTIONS.write_text(json.dumps(old))
    # A retry after rollback can contain a reconstruction in the new artifact
    # and a pregame snapshot in the previous release's artifact for the same game.
    module.PREDICTIONS.write_text(json.dumps({"played": {"kind": "walk_forward_reconstruction", key: new_value}}))
    monkeypatch.setattr(module, "load_feature_rows", lambda **_: rows)
    monkeypatch.setattr(module, "_fit", lambda _: "fitted")
    monkeypatch.setattr(module, "_predict", lambda _, examples: np.full(len(examples), new_value))
    module.train()
    predictions = module.get_predictions()
    assert predictions["played"] == old["played"]
    assert predictions["upcoming"][key] == new_value
    assert json.loads(module.LEGACY_PREDICTIONS.read_text()) == old


def test_strict_market_matching_and_rejects_props():
    game = {"home": "BUF", "away": "MIA", "kickoff": "2026-11-22T18:00:00+00:00"}
    event = {"id": "1", "startTime": game["kickoff"], "teams": [{"abbreviation": "buf"}, {"abbreviation": "mia"}],
             "markets": [{"slug": "full-game", "marketType": "moneyline", "sportsMarketType": "football_team_full_game_winner", "active": True, "closed": False,
                          "description": "Overtime is included. If there is a tie, each side settles at $0.50.",
                          "marketSides": [{"long": True, "team": {"abbreviation": "buf", "ordering": "home"}}, {"long": False, "team": {"abbreviation": "mia", "ordering": "away"}}]}]}
    assert match_event(game, [event]) is not None
    prop = deepcopy(event)
    prop["markets"][0]["sportsMarketType"] = "football_team_first_half_winner"
    assert match_event(game, [prop]) is None
    wrong = deepcopy(event)
    wrong["teams"][0]["abbreviation"] = "nyj"
    assert match_event(game, [wrong]) is None


def test_short_purchase_uses_complement_of_bids_and_depth():
    book = {"bids": [{"px": {"value": "0.70"}, "qty": "40"}, {"px": {"value": "0.65"}, "qty": "60"}],
            "offers": [{"px": {"value": "0.75"}, "qty": "100"}]}
    short = execution(book, False, 100)
    assert short["fillable"]
    assert short["vwap"] == pytest.approx(0.33)
    assert not execution(book, False, 101)["fillable"]


def test_net_edge_subtracts_execution_cost_and_taker_fee():
    quote = {"ask": 0.60, "display_price": 0.59, "execution": {"fillable": True, "vwap": 0.61},
             "fee_coefficient": 0.095, "state": "MARKET_STATE_OPEN"}
    result = valuation(0.65, quote, 100)
    assert result["raw_difference"] == pytest.approx(0.06)
    assert result["gross_edge"] == pytest.approx(0.05)
    assert result["net_edge"] == pytest.approx(0.65-0.61-0.095*0.61*0.39)
    assert result["category"] == "Small potential value"


def test_reference_quote_shows_raw_difference_without_executable_edge():
    quote = {"ask": 0.60, "display_price": 0.58, "state": "REFERENCE_ONLY"}
    result = valuation(0.65, quote, 100)
    assert result["raw_difference"] == pytest.approx(0.07)
    assert result["net_edge"] is None


def test_full_game_total_matching_rejects_team_and_period_totals():
    game = {"home": "BUF", "away": "MIA", "kickoff": "2026-11-22T18:00:00+00:00"}
    market = {"slug": "total-43", "sportsMarketType": "football_team_full_game_total", "marketType": "totals",
              "line": 43.5, "active": True, "closed": False,
              "description": "Both teams combine for over 43.5 points. Overtime is included if played.",
              "marketSides": [{"description": "Over", "long": True}, {"description": "Under", "long": False}]}
    event = {"startTime": game["kickoff"], "teams": [{"abbreviation": "buf"}, {"abbreviation": "mia"}],
             "markets": [market, {**market, "slug": "team", "sportsMarketType": "football_team_points_full_game_total"},
                         {**market, "slug": "half", "sportsMarketType": "football_game_first_half_total"},
                         {**market, "slug": "integer", "line": 44}]}
    assert [m["line"] for m in match_total_markets(game, event)] == [43.5]
    assert match_total_markets(game, {**event, "teams": [{"abbreviation": "nyj"}, {"abbreviation": "mia"}]}) == []
