"""Independent, retrospective pregame full-game score model."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import pickle

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from .data import DATA, TOTAL_FEATURES, load_feature_rows

VERSION = "total-points-hgb-v1"
PREDICTIONS = DATA / "total_predictions.json"
METRICS = DATA / "total_model_metrics.json"
ARTIFACT = DATA / "total_model.pkl"
RESIDUALS = DATA / "total_oos_residuals.json"


def _xy(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray([[r["total_features"][k] for k in TOTAL_FEATURES] for r in rows], dtype=float)
    y = np.asarray([r["home_score"] + r["away_score"] for r in rows], dtype=float)
    return x, y


def _fit(rows: list[dict]) -> HistGradientBoostingRegressor:
    if len(rows) < 300:
        raise RuntimeError("Insufficient completed NFL games for total-points model")
    x, y = _xy(rows)
    model = HistGradientBoostingRegressor(max_iter=140, max_leaf_nodes=10, learning_rate=0.04,
                                          l2_regularization=8, min_samples_leaf=45, random_state=31)
    model.fit(x, y)
    return model


def _predict(model, rows: list[dict]) -> np.ndarray:
    x = np.asarray([[r["total_features"][k] for k in TOTAL_FEATURES] for r in rows], dtype=float)
    return model.predict(x)


def _interval(prediction: float, residuals: list[float]) -> tuple[float | None, float | None]:
    if len(residuals) < 300:
        return None, None
    lo, hi = np.quantile(residuals, [0.1, 0.9])
    return round(max(0, prediction + float(lo)), 1), round(max(0, prediction + float(hi)), 1)


def over_probability(predicted_total: float | None, line: float | None) -> float | None:
    """Empirical survival probability from prior-season out-of-sample errors."""
    if predicted_total is None or line is None or not RESIDUALS.exists():
        return None
    residuals = np.asarray(json.loads(RESIDUALS.read_text()), dtype=float)
    if len(residuals) < 300:
        return None
    # A half-point line cannot push. Integer lines are excluded by market matching.
    return float((np.sum(predicted_total + residuals > line) + 0.5) / (len(residuals) + 1))


def train(refresh_data: bool = False) -> dict:
    rows = load_feature_rows(refresh=refresh_data)
    completed = [r for r in rows if r["home_score"] is not None and r["away_score"] is not None]
    current = max(r["season"] for r in rows)
    oos = {}
    prior_residuals: list[float] = []
    validation_errors: list[float] = []
    validation_hits: list[bool] = []
    by_season = []
    for season in sorted({r["season"] for r in completed}):
        prior = [r for r in completed if r["season"] < season]
        test = [r for r in completed if r["season"] == season]
        if len(prior) < 300 or not test:
            continue
        model = _fit(prior)
        estimates = _predict(model, test)
        actuals = np.asarray([r["home_score"] + r["away_score"] for r in test], dtype=float)
        errors = actuals - estimates
        coverage = []
        for row, estimate, actual in zip(test, estimates, actuals):
            lo, hi = _interval(float(estimate), prior_residuals)
            if lo is not None and season >= 2021 and season < current:
                hit = lo <= actual <= hi
                coverage.append(hit)
                validation_hits.append(hit)
            oos[row["game_id"]] = {"predicted_total": round(float(estimate), 1),
                                   "interval_low": lo, "interval_high": hi,
                                   "kind": "walk_forward_reconstruction", "generated_at": None,
                                   "model_version": VERSION}
        if season >= 2021:
            by_season.append({"season": season, "games": len(test),
                              "mae": round(float(np.mean(np.abs(errors))), 2),
                              "rmse": round(float(np.sqrt(np.mean(errors**2))), 2),
                              "bias": round(float(np.mean(errors)), 2),
                              "interval_coverage": round(float(np.mean(coverage)), 3) if coverage else None})
        if season < current:
            prior_residuals.extend(errors.tolist())
            if season >= 2021:
                validation_errors.extend(errors.tolist())
    final_model = _fit(completed)
    now = datetime.now(timezone.utc).isoformat()
    predictions = dict(oos)
    for row in rows:
        gid = row["game_id"]
        if gid not in predictions and row["home_score"] is None and datetime.fromisoformat(row["kickoff"]) > datetime.now(timezone.utc):
            estimate = float(_predict(final_model, [row])[0])
            lo, hi = _interval(estimate, prior_residuals)
            predictions[gid] = {"predicted_total": round(estimate, 1), "interval_low": lo,
                                "interval_high": hi, "kind": "live_pregame", "generated_at": now,
                                "model_version": VERSION}
    if PREDICTIONS.exists():
        old = json.loads(PREDICTIONS.read_text())
        for row in rows:
            if (datetime.fromisoformat(row["kickoff"]) <= datetime.now(timezone.utc)
                    and old.get(row["game_id"], {}).get("kind") == "live_pregame"):
                predictions[row["game_id"]] = old[row["game_id"]]
    validation = {"model_version": VERSION, "generated_at": now,
                  "method": "Season walk-forward regression; each test season uses earlier seasons only. The 80% interval for a test season uses earlier out-of-sample residuals.",
                  "seasons": by_season,
                  "aggregate": {"games": len(validation_errors),
                                "mae": round(float(np.mean(np.abs(validation_errors))), 2) if validation_errors else None,
                                "rmse": round(float(np.sqrt(np.mean(np.asarray(validation_errors)**2))), 2) if validation_errors else None,
                                "bias": round(float(np.mean(validation_errors)), 2) if validation_errors else None,
                                "interval_coverage": round(float(np.mean(validation_hits)), 3) if validation_hits else None},
                  "note": "Full-game points include overtime. Historical totals-market prices are not available here, so this is score validation, not a trading backtest. Historical forecasts are reconstructions."}
    for path, payload in ((PREDICTIONS, predictions), (METRICS, validation), (RESIDUALS, prior_residuals)):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, allow_nan=False))
        tmp.replace(path)
    with ARTIFACT.open("wb") as f:
        pickle.dump({"model": final_model, "features": TOTAL_FEATURES, "version": VERSION}, f)
    return validation


def get_predictions() -> dict:
    if not PREDICTIONS.exists():
        raise RuntimeError("Total-points model is preparing. Try again shortly.")
    return json.loads(PREDICTIONS.read_text())


def get_metrics() -> dict:
    if not METRICS.exists():
        raise RuntimeError("Total-points validation is preparing. Try again shortly.")
    return json.loads(METRICS.read_text())
