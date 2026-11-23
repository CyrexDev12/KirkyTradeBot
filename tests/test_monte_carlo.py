from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import numpy as np
import pytest

from backend import mc_archive
from backend.markets import _tie_payout
from backend.mc_validation import evaluate
from backend.mc_value import market_values
from backend.monte_carlo import (Config, Ratings, Snapshot, completed_inputs, date, history,
                                 simulate, total_probabilities, week_cutoff, weights)


@pytest.fixture
def schedule():
    start = datetime(2025, 9, 7, 17, tzinfo=timezone.utc)
    rng = np.random.default_rng(4)
    rows = []
    for week in range(1, 14):
        teams = rng.permutation(16).tolist()
        for i in range(0, 16, 2):
            home, away = teams[i:i+2]
            hs, gamescore = (int(x) for x in rng.integers(7, 40, size=2))
            rows.append(dict(game_id=f"2025_{week:02d}_{i:02d}", season=2025, week=week, game_type="REG",
                             kickoff=(start+timedelta(weeks=week-1)).isoformat(), neutral_site=i==0,
                             home=f"T{home}", away=f"T{away}", home_score=hs, away_score=gamescore,
                             result=hs-gamescore, kickoff_confirmed=True))
    return rows


def snapshot(rows, game, cfg=Config()):
    cutoff = week_cutoff(date(game["kickoff"]))
    return Snapshot(history(completed_inputs(rows, game["season"]), game, cutoff), cutoff, cfg)


def test_snapshots_exclude_future_wrong_season_unfinished_and_target(schedule):
    target = schedule[-9]
    f = snapshot(schedule, target).forecast(target, "reconstruction")
    extra = {**schedule[0], "game_id":"older", "season":2024, "home_score":300, "result":280}
    changed = [{**g, "home_score":200, "result":200-g["away_score"]} if g["week"] >= target["week"]
               else g for g in schedule]
    deferred = {**schedule[0], "game_id":"late", "result_available_at":target["kickoff"]}
    partial = {**schedule[0], "game_id":"partial", "source_status":"in progress"}
    incomplete = {**schedule[0], "game_id":"incomplete", "away_score":None}
    assert snapshot([*reversed(changed), extra, deferred, partial, incomplete, schedule[0]], target).forecast(
        target, "reconstruction") == f
    assert f["status"] == "available" and target["game_id"] not in f["input_game_ids"]
    assert snapshot(schedule,schedule[0]).forecast(schedule[0],"reconstruction")["unavailable_reason"] == "Insufficient current-season data"
    assert snapshot(schedule,target,replace(Config(),min_residual_effective_n=200)).forecast(target,"reconstruction")["status"] == "unavailable"


def test_weights_and_forward_bank(schedule):
    target = schedule[-1]
    snap = snapshot(schedule,target)
    fit = snap.ratings
    assert sum(fit.effects(t)[0] for t in fit.teams) == pytest.approx(0, abs=1e-10)
    assert sum(fit.effects(t)[1] for t in fit.teams) == pytest.approx(0, abs=1e-10)
    w = weights([schedule[0],schedule[32]], snap.cutoff, snap.cfg)
    assert w[0]/w[1] == pytest.approx(.5)
    ids = {g["game_id"]:g for g in snap.games}
    for pair in snap.bank:
        g = ids[pair["game_id"]]
        cutoff = date(pair["as_of"])
        assert cutoff < date(g["kickoff"])
        earlier = history(snap.games,g,cutoff)
        assert all(date(x["released_at"]) <= cutoff and x["week"] < g["week"] for x in earlier)
        assert Ratings(earlier,cutoff,snap.cfg).means(g) == pytest.approx(pair["predicted"])
    expected = fit.means(target)
    fit.coef[2+len(fit.teams)+fit.index[target["away"]]] += 3
    assert fit.means(target) == pytest.approx(expected - [3,0])


def test_extreme_pregame_input_has_bounded_score_and_residual_influence(schedule):
    target = schedule[-1]
    normal = snapshot(schedule, target)
    edited = deepcopy(schedule)
    outlier = next(g for g in edited if g["week"] == target["week"] - 1 and target["home"] in (g["home"], g["away"]))
    side = "home" if outlier["home"] == target["home"] else "away"
    outlier[f"{side}_score"] = 180
    outlier["result"] = outlier["home_score"] - outlier["away_score"]
    robust = snapshot(edited, target)
    f = robust.forecast(target, "reconstruction")
    team = f["teams"][target["home"]]
    assert team["adjusted_scoring_games"] >= 1
    assert team["weighted_for"] < team["points_for"]
    assert abs(f["fitted_score_means"][0] - normal.ratings.means(target)[0]) < 5
    assert f["diagnostics"]["adjusted_residual_pairs"] >= 1
    assert max(max(pair["actual"]) for pair in robust.bank) == 180
    assert max(h + a for h, a, _ in f["paired_score_counts"]) < outlier["home_score"] + outlier["away_score"]


def test_january_season_cutoff_and_invalid_data(schedule):
    january = {**schedule[0],"kickoff":"2026-01-04T18:00:00+00:00","week":18}
    assert len(completed_inputs([january],2025)) == 1
    assert completed_inputs([january],2026) == []
    assert week_cutoff(date(january["kickoff"])).isoformat() == "2025-12-30T17:00:00+00:00"
    for patch in ({"source_status":"abandoned"},{"neutral_site":None},{"kickoff_confirmed":False},
                  {"home_score":float("nan")},{"home_score":-1},{"result":999},
                  {"result_available_at":"2026-01-05T10:00:00"},{"result_available_at":"unknown"}):
        assert completed_inputs([{**january,**patch}],2025) == []
    assert completed_inputs([january,{**january,"home_score":100,"result":100-january["away_score"]}],2025) == []
    with pytest.raises(ValueError):
        Config(half_life_days=0)


def test_pathological_fit_returns_a_structured_quality_error(schedule):
    game = schedule[-1]
    forecast = snapshot(schedule,game,replace(Config(),half_life_days=1e-6)).forecast(game,"reconstruction")
    assert forecast["status"] == "unavailable"
    assert forecast["unavailable_reason"] == "Scoring fit failed numerical checks"


def test_common_draws_coherent_scores_and_exact_neutral_symmetry():
    errors = np.array([[-10,5],[3,2],[9,-1],[2,1]])
    weights_ = np.array([1.,.5,3,2])
    n = 50_000
    f = simulate([23,23],errors,weights_,True,n,123)
    eq = f["diagnostics"]["exact_outcome_probabilities"]
    assert eq[0] == pytest.approx(eq[1])
    assert f["diagnostics"]["max_sampling_difference"] < .01
    assert sum(f[k] for k in ("probability_home_win","probability_away_win","probability_tie")) == pytest.approx(1)
    d = f["distributions"]
    assert d["total"]["mean"] == pytest.approx(d["home"]["mean"]+d["away"]["mean"])
    assert d["margin"]["mean"] == pytest.approx(d["home"]["mean"]-d["away"]["mean"])
    assert simulate([26,23],errors,weights_,True,n,123)["probability_home_win"] >= f["probability_home_win"]
    forecast = {**f, "status":"available", "simulation_count":n}
    lines = [total_probabilities(forecast,line) for line in (36.5,42.5,50.5,65.5)]
    assert all(p["over"]+p["under"] == 1 for p in lines)
    assert [p["over"] for p in lines] == sorted((p["over"] for p in lines),reverse=True)
    assert total_probabilities(forecast,42) is None
    # Perfectly anticorrelated paired templates have one possible total.
    anti = simulate([20.5,20.5],np.array([[-3,3],[3,-3]]),np.ones(2),False,1000,3)
    assert anti["distributions"]["total"]["histogram"] == [[42,1000]]
    bounded = simulate([1,1],np.array([[-10,10],[10,-10]]),np.ones(2),False,1000,3)
    assert bounded["diagnostics"]["clipped_draw_rate"] == 1
    assert bounded["distributions"]["home"]["histogram"][0][0] == 0


def test_archive_freeze_config_refresh_and_concurrency(schedule,tmp_path,monkeypatch):
    target = {**schedule[-1], "home_score":None, "away_score":None, "result":None}
    rows = [*schedule[:-1],target]
    kickoff = date(target["kickoff"])
    now = kickoff - timedelta(days=1)
    path = tmp_path/"mc.sqlite3"
    def fetch(g=target, cfg=Config()):
        return mc_archive.forecasts(rows,2025,[g],path=path,config=cfg,clock=lambda:now)[g["game_id"]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        f, again = list(pool.map(lambda _: fetch(), range(2)))
    assert f == again and f["status"] == "available"
    now += timedelta(minutes=2)
    assert fetch() == f
    modified = fetch(cfg=replace(Config(),half_life_days=21))
    assert f["random_seed"] != modified["random_seed"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts WHERE game=?", (target["game_id"],)).fetchone()[0] == 2
        inputs = json.loads(db.execute("SELECT inputs FROM snapshots WHERE hash=?",(f["data_snapshot_hash"],)).fetchone()[0])
        assert all(g["observed_at"] <= f["as_of"] for g in inputs)
    now = kickoff
    assert fetch() == modified
    later = {**target,"kickoff":(kickoff+timedelta(days=5)).isoformat()}
    rows[-1] = later
    assert fetch(later) == modified
    missing = fetch(schedule[-2])
    assert missing["unavailable_reason"] == "No archived pregame MC prediction"
    assert fetch({**target, "game_type":"WC"})["unavailable_reason"] == "Regular-season games only"
    changed = deepcopy(rows)
    changed[0]["home_score"] += 3
    changed[0]["result"] += 3
    rows[:] = changed
    assert fetch(later) == modified


def test_generation_crossing_kickoff_does_not_create_archive(schedule,tmp_path):
    game = {**schedule[-1],"home_score":None,"away_score":None,"result":None}
    schedule[-1] = game
    kickoff = date(game["kickoff"])
    times = iter([kickoff-timedelta(milliseconds=1),kickoff])
    f = mc_archive.forecasts(schedule,2025,[game],path=tmp_path/"mc.db",clock=lambda:next(times))[game["game_id"]]
    assert f["unavailable_reason"] == "No archived pregame MC prediction"


def test_verified_schedule_change_before_kickoff_refreshes_archive(schedule,tmp_path):
    g = {**schedule[-1],"home_score":None,"away_score":None,"result":None}
    schedule[-1] = g
    now = date(g["kickoff"]) - timedelta(days=2)
    path = tmp_path/"mc.db"
    f = mc_archive.forecasts(schedule,2025,[g],path=path,clock=lambda:now)[g["game_id"]]
    moved = {**g,"kickoff":(date(g["kickoff"])+timedelta(days=1)).isoformat(),"neutral_site":not g["neutral_site"]}
    rows = [*schedule[:-1],moved]
    new = mc_archive.forecasts(rows,2025,[moved],path=path,clock=lambda:now)[g["game_id"]]
    assert new["random_seed"] != f["random_seed"]
    assert new["kickoff"] == moved["kickoff"]


def test_settlement_and_execution_use_separate_fields(schedule):
    g = schedule[-1]
    f = snapshot(schedule,g).forecast(g,"reconstruction")
    before = deepcopy(f)
    quote = dict(ask=.4, display_price=.42, state="MARKET_STATE_OPEN", fee_coefficient=.02,
                 execution=dict(fillable=True, vwap=.4, fills=[dict(price=.4,quantity=100)]))
    market = dict(outcomes={g["home"]:quote,g["away"]:quote},tie_payout=.5)
    totals = {"lines":[dict(outcomes={"over":quote,"under":quote},line=44.5,status="REFERENCE_ONLY",
                            full_game_half_point_payout=False)]}
    values = market_values(f,market,totals,100)
    expected = f["probability_home_win"]+.5*f["probability_tie"]
    home = values["moneyline"][g["home"]]
    assert home["fair_value"] == pytest.approx(expected)
    assert home["net_edge"] == pytest.approx(expected-.4-home["estimated_fee_per_contract"])
    assert "category" not in home and values["totals"][0]["over"] is None
    for state in ("REFERENCE_ONLY","STALE","MARKET_STATE_CLOSED"):
        quote["state"] = state
        assert market_values(f,market,None,30)["moneyline"][g["home"]]["net_edge"] is None
    assert not market_values(f,{**market,"tie_payout":None},None,100)["moneyline"]
    assert _tie_payout({"description":"In a tie tickets are refunded. Fees can be $0.50."}) is None
    assert _tie_payout({"description":"If the game ends in a tie, the market will settle to $0.50."}) == .5
    assert f == before


def test_evaluation_matches_only_held_out_predictions(schedule):
    target = schedule[-1]
    artifacts = {target["game_id"]:dict(kind="walk_forward_reconstruction",home_probability=.5),
                 schedule[-2]["game_id"]:dict(kind="live_pregame",home_probability=1)}
    report = evaluate(schedule,2025,Config(),existing=artifacts)
    assert report["eligible"] < report["completed"]
    if target["home_score"] != target["away_score"]:
        assert report["matched_binary"]["game_ids"] == [target["game_id"]]
    assert report["matched_totals"] is None
    assert 0 <= report["metrics"]["intervals"]["total"]["80"]["coverage"] <= 1


def test_api_mode_defaults_and_mc_route(schedule,monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as api
    g = schedule[-1]
    f = snapshot(schedule,g).forecast(g,"reconstruction")
    calls = []
    def forecasts(season, selected):
        calls.append(season)
        return {g["game_id"]:f}
    async def markets(*args,**kwargs):
        return dict(markets={},fetched_at=None,error=None)
    async def totals(*args,**kwargs):
        return dict(totals={},fetched_at=None,error=None)
    monkeypatch.setattr(api,"_all_games",lambda:[g])
    monkeypatch.setattr(api,"get_forecasts",forecasts)
    monkeypatch.setattr(api,"_predict_data",lambda:({g["game_id"]:dict(home_probability=.6)},None))
    monkeypatch.setattr(api,"_total_predict_data",lambda:({},None))
    monkeypatch.setattr(api,"get_week_markets",markets)
    monkeypatch.setattr(api,"get_week_totals",totals)
    client = TestClient(api.app)
    url = "/api/dashboard?season=2025&week=13"
    old = client.get(url).json()["games"][0]
    assert old["prediction"]["home_probability"] == .6 and "monte_carlo" not in old
    assert not calls
    compare = client.get(url+"&model=compare").json()["games"][0]
    assert compare["monte_carlo"]["probability_home_win"] == f["probability_home_win"]
    assert compare["prediction"] == old["prediction"]
    assert client.get(url+"&model=monte_carlo").json()["games"][0]["prediction"] is None
    assert client.get(url+"&model=bad").status_code == 422
    assert client.get("/api/monte-carlo/predictions?season=2025&week=13").json()["predictions"][g["game_id"]]["status"] == "available"
    assert client.get("/api/games/missing/monte-carlo").status_code == 404
