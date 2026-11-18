"""Rolling, previous-week evaluation. Run python -m backend.mc_validation --help."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sqlite3

import numpy as np

from .mc_archive import DB, get_forecasts, season_rows
from .model import PREDICTIONS
from .totals import PREDICTIONS as TOTAL_PREDICTIONS
from .monte_carlo import (MODEL_ID, VERSION, Config, Snapshot, completed_inputs, date,
                          history, total_probabilities, week_cutoff)

LOG_EPS = 1e-6
TOTAL_LINES = (37.5, 44.5, 51.5)


def calibration(p, y):
    bins = np.minimum((p * 10).astype(int), 9)
    return [dict(bin=int(i), games=int((bins == i).sum()), predicted=p[bins == i].mean(),
                 observed=y[bins == i].mean()) for i in np.unique(bins)]


def binary(p, y):
    if not len(p):
        return None
    p, y = np.asarray(p), np.asarray(y)
    clipped = np.clip(p, LOG_EPS, 1-LOG_EPS)
    return dict(games=len(y), brier=np.mean((p-y)**2), accuracy=np.mean((p >= .5) == y),
                log_loss=-np.mean(y*np.log(clipped)+(1-y)*np.log1p(-clipped)), calibration=calibration(p, y))


def score_errors(predicted, actual):
    err = np.asarray(predicted) - actual
    return dict(mae=np.abs(err).mean(), rmse=np.sqrt(np.mean(err**2)), bias=err.mean())


def compatible_legacy_inputs(all_games, target, inputs):
    # The legacy feature builder updates only the two participating teams after a game.
    # Accept an identical season prefix, plus any intervening games involving neither
    # target team. No later feature used by either target can depend on that suffix.
    prior = [g for g in all_games if (g["kickoff"], g["game_id"]) < (target["kickoff"], target["game_id"])
             and g["home_score"] is not None and g["away_score"] is not None]
    if [g["game_id"] for g in prior[:len(inputs)]] != [g["game_id"] for g in inputs]:
        return False
    teams = {target["home"], target["away"]}
    return all(teams.isdisjoint((g["home"], g["away"])) for g in prior[len(inputs):])


def evaluate(rows, season, cfg, *, existing=None, totals=None, now=None):
    now = now or datetime.now(timezone.utc)
    rows = sorted((g for g in rows if g["season"] == season and g["game_type"] == "REG"),
                  key=lambda g: (g["kickoff"], g["game_id"]))
    games = [g for g in completed_inputs(rows, season) if date(g["released_at"]) <= now]
    snaps, held_out = {}, []
    for g in games:
        cutoff = week_cutoff(date(g["kickoff"]))
        key = (cutoff, g["week"])
        if key not in snaps:
            snaps[key] = Snapshot(history(games, g, cutoff), cutoff, cfg)
        snap = snaps[key]
        f = snap.forecast(g, "reconstruction")
        if f["status"] == "available":
            held_out.append((g, f, compatible_legacy_inputs(rows, g, snap.games)))
    report = dict(model_id=MODEL_ID, model_version=VERSION, forecast_origin="reconstruction", season=season, as_of=now.isoformat(),
                  config=asdict(cfg), log_loss_clip=LOG_EPS, scheduled=len(rows), completed=len(games),
                  eligible=len(held_out), coverage=len(held_out)/len(games) if games else None,
                  metrics=None, matched_binary=None, matched_totals=None)
    if not held_out:
        return report
    forecasts = [f for _, f, _ in held_out]
    actual = np.array([[g["home_score"], g["away_score"]] for g, _, _ in held_out])
    h, a = actual.T
    actuals = dict(home=h, away=a, total=h+a, margin=h-a)
    p = np.array([[f["probability_home_win"], f["probability_away_win"], f["probability_tie"]] for f in forecasts])
    truth = np.stack((h>a, h<a, h==a), axis=1)
    metrics = dict(three_way_brier=np.sum((p-truth)**2, axis=1).mean(), predicted_tie_rate=p[:,2].mean(),
                   observed_tie_rate=truth[:,2].mean(), clipped_draw_rate=np.mean([f["diagnostics"]["clipped_draw_rate"] for f in forecasts]),
                   score_errors={}, intervals={}, baseline_errors={}, total_lines={})
    baseline = np.array([f["baseline_means"] for f in forecasts]).T
    for k, base in zip(actuals, (*baseline, baseline.sum(axis=0), baseline[0]-baseline[1])):
        summaries = [f["distributions"][k] for f in forecasts]
        metrics["score_errors"][k] = score_errors([s["mean"] for s in summaries], actuals[k])
        metrics["baseline_errors"][k] = score_errors(base, actuals[k])
        metrics["intervals"][k] = {}
        for level in (50, 80):
            lo, hi = np.array([s[f"interval_{level}"] for s in summaries]).T
            metrics["intervals"][k][str(level)] = dict(coverage=np.mean((lo <= actuals[k]) & (actuals[k] <= hi)),
                                                      width=np.mean(hi-lo), below=np.mean(actuals[k] < lo),
                                                      above=np.mean(actuals[k] > hi))
    for line in TOTAL_LINES:
        metrics["total_lines"][str(line)] = binary([total_probabilities(f, line)["over"] for f in forecasts], h+a > line)
    eligible_binary = [(i, f["home_win_given_no_tie"]) for i, f in enumerate(forecasts)
                       if h[i] != a[i] and f["home_win_given_no_tie"] is not None]
    metrics["binary_no_tie"] = binary([p for _, p in eligible_binary], [h[i]>a[i] for i, _ in eligible_binary])
    report["metrics"] = metrics
    # These versioned artifacts already hold season-held-out predictions. Never
    # substitute a production fit, or a live artifact generated at another cutoff.
    def matched(artifacts, indices):
        return [(i, artifacts[g["game_id"]]) for i in indices for g, _, ok in [held_out[i]]
                if ok and (artifacts.get(g["game_id"]) or {}).get("kind") == "walk_forward_reconstruction"]
    pairs = matched(existing or {}, [i for i, _ in eligible_binary])
    if pairs:
        y = [h[i]>a[i] for i, _ in pairs]
        report["matched_binary"] = dict(game_ids=[held_out[i][0]["game_id"] for i, _ in pairs],
            mc=binary([forecasts[i]["home_win_given_no_tie"] for i, _ in pairs], y),
            existing=binary([old["home_probability"] for _, old in pairs], y))
    pairs = matched(totals or {}, range(len(h)))
    if pairs:
        idx = [i for i, _ in pairs]
        y = (h+a)[idx]
        report["matched_totals"] = dict(game_ids=[held_out[i][0]["game_id"] for i in idx],
            mc=score_errors([forecasts[i]["distributions"]["total"]["mean"] for i in idx], y),
            weighted_ppg=score_errors(baseline.sum(axis=0)[idx], y),
            existing=score_errors([old["predicted_total"] for _, old in pairs], y))
    return report


def metrics(season, now=None):
    return evaluate(season_rows(season), season, Config.from_env(), now=now,
                    existing=json.loads(PREDICTIONS.read_text()) if PREDICTIONS.exists() else {},
                    totals=json.loads(TOTAL_PREDICTIONS.read_text()) if TOTAL_PREDICTIONS.exists() else {})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--evaluate", action="store_true", help="Reconstruct and score each eligible earlier week")
    action.add_argument("--game", help="Print an archived forecast, its inputs and paired error bank")
    parser.add_argument("--as-of", help="End a reconstruction at an ISO timestamp with UTC offset")
    args = parser.parse_args()
    if args.as_of and not args.evaluate:
        parser.error("--as-of requires --evaluate")
    if args.evaluate:
        cutoff = datetime.fromisoformat(args.as_of) if args.as_of else None
        if cutoff is not None and cutoff.tzinfo is None:
            parser.error("--as-of requires a UTC offset")
        result = metrics(args.season, cutoff)
    else:
        rows = season_rows(args.season)
        selected = [g for g in rows if g["game_id"] == args.game] if args.game else rows
        if not selected:
            parser.error("No such game or season in the schedule")
        records = get_forecasts(args.season, selected)
        if args.game:
            f = records[args.game]
            result = {"forecast": f}
            if f.get("data_snapshot_hash"):
                with closing(sqlite3.connect(DB)) as db:
                    result["inputs"] = json.loads(db.execute("SELECT inputs FROM snapshots WHERE hash=?",
                                                             (f["data_snapshot_hash"],)).fetchone()[0])
                    result["reconstructed_error_pairs"] = json.loads(db.execute(
                        "SELECT pairs FROM banks WHERE snapshot=? AND cutoff=? AND config=? AND version=?",
                        (f["data_snapshot_hash"], f["as_of"], f["config_hash"], f["model_version"])).fetchone()[0])
        else:
            result = {"season": args.season, "games": len(records),
                      "available": sum(f["status"] == "available" for f in records.values())}
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
