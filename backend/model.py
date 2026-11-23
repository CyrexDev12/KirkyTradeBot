"""Time-ordered NFL model fitting, validation, and cached predictions."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import pickle

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss

from .data import DATA, FEATURES, MODEL_VERSION, frozen_pregame_predictions, load_feature_rows

LEGACY_PREDICTIONS = DATA / "predictions.json"
PREDICTIONS = DATA / f"predictions_{MODEL_VERSION}.json"
METRICS = DATA / f"model_metrics_{MODEL_VERSION}.json"
ARTIFACT = DATA / f"model_{MODEL_VERSION}.pkl"


def needs_refresh() -> bool:
    now = datetime.now(timezone.utc).timestamp()
    try:
        return now - min(path.stat().st_mtime for path in (PREDICTIONS, METRICS, ARTIFACT)) > 6 * 3600
    except FileNotFoundError:
        return True


def _xy(rows: list[dict]):
    x = np.asarray([[r["features"][k] for k in FEATURES] for r in rows], dtype=float)
    y = np.asarray([int(r["home_score"] > r["away_score"]) for r in rows], dtype=int)
    return x, y


def _fit(rows: list[dict]):
    x, y = _xy(rows)
    if len(rows) < 300 or len(set(y)) < 2:
        raise RuntimeError("Insufficient completed NFL games to fit a model")
    model = HistGradientBoostingClassifier(max_iter=170, max_leaf_nodes=12, learning_rate=0.045,
                                           l2_regularization=4.0, min_samples_leaf=35, random_state=17)
    model.fit(x, y)
    return model


def _predict(model, rows: list[dict]) -> np.ndarray:
    x = np.asarray([[r["features"][k] for k in FEATURES] for r in rows], dtype=float)
    return model.predict_proba(x)[:, 1]


def _calibrate(prob: np.ndarray, calibrator) -> np.ndarray:
    if calibrator is None:
        return prob
    logits = np.log(np.clip(prob, 1e-6, 1-1e-6) / np.clip(1-prob, 1e-6, 1-1e-6))
    return calibrator.predict_proba(logits.reshape(-1, 1))[:, 1]


def train(refresh_data: bool = False) -> dict:
    rows = load_feature_rows(refresh=refresh_data)
    completed = [r for r in rows if r["home_score"] is not None and r["away_score"] is not None and r["home_score"] != r["away_score"]]
    current = max(r["season"] for r in rows)
    # Season-by-season walk-forward: each test season is unseen during fitting.
    oos: dict[str, dict] = {}
    by_season = []
    past_oos_p, past_oos_y = [], []
    validation_p, validation_y = [], []
    for season in sorted({r["season"] for r in completed}):
        prior = [r for r in completed if r["season"] < season]
        test = [r for r in completed if r["season"] == season]
        if len(prior) < 300 or not test:
            continue
        model = _fit(prior)
        raw = _predict(model, test)
        y = np.asarray([int(r["home_score"] > r["away_score"]) for r in test])
        year_calibrator = None
        if len(past_oos_p) >= 300:
            source = np.clip(np.asarray(past_oos_p), 1e-6, 1-1e-6)
            logits = np.log(source / (1-source))
            year_calibrator = LogisticRegression(random_state=17).fit(logits.reshape(-1, 1), np.asarray(past_oos_y))
        p = _calibrate(raw, year_calibrator)
        if season >= 2021 and season < current:
            validation_p.extend(p.tolist())
            validation_y.extend(y.tolist())
        if season < current:
            past_oos_p.extend(raw.tolist())
            past_oos_y.extend(y.tolist())
        if season >= 2021:
            by_season.append({"season": season, "games": len(test),
                              "brier": round(float(brier_score_loss(y, p)), 4),
                              "log_loss": round(float(log_loss(y, p, labels=[0, 1])), 4),
                              "accuracy": round(float(accuracy_score(y, p >= 0.5)), 4)})
        for row, prob in zip(test, p):
            oos[row["game_id"]] = {"home_probability": float(prob), "kind": "walk_forward_reconstruction",
                                   "generated_at": None, "model_version": MODEL_VERSION}
    calibrator = None
    if len(past_oos_p) >= 300:
        p = np.clip(np.asarray(past_oos_p), 1e-6, 1-1e-6)
        logits = np.log(p / (1-p))
        calibrator = LogisticRegression(random_state=17).fit(logits.reshape(-1, 1), np.asarray(past_oos_y))
    final_model = _fit(completed)
    now = datetime.now(timezone.utc).isoformat()
    predictions = {}
    for row in rows:
        gid = row["game_id"]
        if gid in oos:
            predictions[gid] = oos[gid]
        elif row["home_score"] is None and datetime.fromisoformat(row["kickoff"]) > datetime.now(timezone.utc):
            raw = _predict(final_model, [row])
            prob = float(_calibrate(raw, calibrator)[0])
            predictions[gid] = {"home_probability": prob, "kind": "live_pregame", "generated_at": now,
                                "model_version": MODEL_VERSION}
    predictions.update(frozen_pregame_predictions(rows, LEGACY_PREDICTIONS, PREDICTIONS))
    selected = [r for r in by_season if r["season"] < current]
    validation = {"model_version": MODEL_VERSION, "generated_at": now, "method": "season walk-forward; both the model and probability calibrator use earlier seasons only",
                  "seasons": by_season, "aggregate": None, "calibration": [], "trading_backtest": None,
                  "note": "Historical prices were not fabricated. Walk-forward historical probabilities are reconstructions, not contemporaneously archived forecasts."}
    if selected:
        total = sum(r["games"] for r in selected)
        validation["aggregate"] = {k: round(sum(r[k]*r["games"] for r in selected)/total, 4) for k in ("brier", "log_loss", "accuracy")}
        validation["aggregate"]["games"] = total
    if validation_p:
        p, y = np.asarray(validation_p), np.asarray(validation_y)
        for lo in np.linspace(0, 0.9, 10):
            mask = (p >= lo) & (p < lo + 0.1 if lo < 0.9 else p <= 1)
            if mask.sum():
                validation["calibration"].append({"predicted": round(float(p[mask].mean()), 3),
                                                  "observed": round(float(y[mask].mean()), 3), "games": int(mask.sum())})
    for path, payload in ((PREDICTIONS, predictions), (METRICS, validation)):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    tmp_artifact = ARTIFACT.with_suffix(".tmp")
    with tmp_artifact.open("wb") as f:
        pickle.dump({"model": final_model, "calibrator": calibrator, "features": FEATURES, "version": MODEL_VERSION}, f)
    tmp_artifact.replace(ARTIFACT)
    return {"games": len(rows), "predictions": len(predictions), "validation": validation["aggregate"]}


def get_predictions() -> dict:
    if not PREDICTIONS.exists():
        raise RuntimeError("Model has not been trained. Run python -m backend.prepare")
    return json.loads(PREDICTIONS.read_text())


def get_metrics() -> dict:
    if not METRICS.exists():
        raise RuntimeError("Model has not been trained. Run python -m backend.prepare")
    return json.loads(METRICS.read_text())
