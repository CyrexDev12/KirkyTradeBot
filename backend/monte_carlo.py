"""Current-season paired score model. Only schedule results enter this module."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from zoneinfo import ZoneInfo

import numpy as np

MODEL_ID = "current_season_monte_carlo"
VERSION = "paired-scores-v1.0"
EASTERN = ZoneInfo("America/New_York")
TARGET_FIELDS = ("game_id", "season", "week", "game_type", "home", "away", "kickoff", "neutral_site")


@dataclass(frozen=True)
class Config:
    simulations: int = 50_000
    half_life_days: float = 28.0
    team_ridge: float = 4.0
    home_ridge: float = 16.0
    min_team_games: int = 4
    residual_warmup_team_games: int = 2
    min_residual_pairs: int = 40
    min_residual_effective_n: float = 25.0
    base_seed: int = 17

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if not math.isfinite(value) or value < 0 or (value == 0 and field.name != "base_seed"):
                raise ValueError(f"Invalid NFL_MC_{field.name.upper()}")

    @classmethod
    def from_env(cls):
        return cls(**{f.name: type(f.default)(os.getenv("NFL_MC_" + f.name.upper(), f.default))
                      for f in fields(cls)})


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("A timestamp must include its UTC offset")
    return parsed.astimezone(timezone.utc)


def week_cutoff(kickoff: datetime) -> datetime:
    """Tuesday noon Eastern preceding kickoff, including the DST offset on that Tuesday."""
    local = kickoff.astimezone(EASTERN)
    boundary = (local - timedelta(days=(local.weekday() - 1) % 7)).replace(hour=12, minute=0, second=0, microsecond=0)
    if boundary > local:
        boundary -= timedelta(days=7)
    return boundary.astimezone(timezone.utc)


def completed_inputs(rows, season: int) -> list[dict]:
    """Strip market columns and reject unfinished, ambiguous or duplicate results."""
    by_id = {}
    for row in rows:
        if row["season"] != season or row["game_type"] != "REG":
            continue
        score = (row["home_score"], row["away_score"])
        status = str(row.get("source_status") or "").strip().lower()
        if (not all(isinstance(x, (int, float)) and math.isfinite(x) and x >= 0 and x == int(x) for x in score)
                or row.get("neutral_site") is None or not row.get("kickoff_confirmed", True)
                or status and status not in {"final", "completed", "final/ot"}
                or row.get("result") != score[0] - score[1]):
            continue
        try:
            kickoff = date(row["kickoff"])
            if row.get("result_available_at"):
                released = date(str(row["result_available_at"]))
                if released <= kickoff:
                    continue
            else:
                # The schedule has no historical final whistle/arrival timestamp.
                released = (week_cutoff(kickoff).astimezone(EASTERN) + timedelta(weeks=1)).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
        game = {k: row[k] for k in TARGET_FIELDS}
        game.update(home_score=int(score[0]), away_score=int(score[1]), released_at=released.isoformat())
        gid = row["game_id"]
        if gid in by_id and by_id[gid] != game:
            by_id[gid] = None
        else:
            by_id[gid] = game
    return sorted((g for g in by_id.values() if g), key=lambda g: (g["kickoff"], g["game_id"]))


def history(games, target, cutoff, *, live=False):
    return [g for g in games if g["season"] == target["season"] and g["week"] < target["week"]
            and g["game_id"] != target["game_id"] and date(g["released_at"]) <= cutoff
            and (not live or date(g["observed_at"]) <= cutoff)]


def weights(games, cutoff, cfg):
    ages = [(cutoff - date(g["kickoff"])).total_seconds() / 86400 for g in games]
    return np.exp2(-np.array(ages) / cfg.half_life_days)


def effective_n(w):
    return float(w.sum() ** 2 / (w @ w)) if len(w) else 0.0


class Ratings:
    def __init__(self, games, cutoff, cfg):
        self.games = games
        self.weights = weights(games, cutoff, cfg)
        self.teams = sorted({g[role] for g in games for role in ("home", "away")})
        self.index = {team: i for i, team in enumerate(self.teams)}
        n = len(self.teams)
        x = np.zeros((2 * len(games), 2 + 2 * n))
        y = np.empty(2 * len(games))
        for i, g in enumerate(games):
            for side, (own, opp, venue) in enumerate([("home", "away", .5), ("away", "home", -.5)]):
                row = 2 * i + side
                x[row, 0] = 1
                x[row, 1] = 0 if g["neutral_site"] else venue
                x[row, 2 + self.index[g[own]]] = 1
                x[row, 2 + n + self.index[g[opp]]] = -1
                y[row] = g[own + "_score"]
        # With a free intercept and equal positive team penalties, translating either
        # family into the intercept strictly reduces its penalty unless its mean is zero.
        wx = x.T * self.weights.repeat(2)
        penalty = np.diag([0, cfg.home_ridge] + [cfg.team_ridge] * (2 * n))
        self.coef = np.linalg.solve(wx @ x + penalty, wx @ y)
        if not np.isfinite(self.coef).all():
            raise ValueError("Nonfinite fitted coefficients")
        self.counts = {team: sum(team in (g["home"], g["away"]) for g in games) for team in self.teams}

    def effects(self, team):
        i = self.index[team]
        return self.coef[2+i], self.coef[2+len(self.teams)+i]

    def means(self, game):
        ho, hd = self.effects(game["home"])
        ao, ad = self.effects(game["away"])
        baseline, venue = self.coef[:2]
        venue = 0 if game["neutral_site"] else venue / 2
        return np.array([baseline + ho - ad + venue, baseline + ao - hd - venue])

    def summary(self, team):
        pf, pa, used = [], [], {}
        for g, w in zip(self.games, self.weights):
            if team in (g["home"], g["away"]):
                home = g["home"] == team
                pf.append(g["home_score"] if home else g["away_score"])
                pa.append(g["away_score"] if home else g["home_score"])
                used[g["game_id"]] = float(w)
        w = np.array(list(used.values()))
        offense, defense = self.effects(team)
        return dict(games=len(w), effective_n=effective_n(w), points_for=np.mean(pf), points_allowed=np.mean(pa),
                    weighted_for=np.average(pf, weights=w), weighted_allowed=np.average(pa, weights=w),
                    scoring_strength=offense, score_prevention=defense, game_weights=used)


class Snapshot:
    """A common cutoff and eligible games can serve a whole slate."""
    def __init__(self, games, cutoff, cfg):
        self.games, self.cutoff, self.cfg = games, cutoff, cfg
        self.hash = digest(games)
        self.bank = []
        self.bank_weights = np.array([])
        self.fit_error = None
        try:
            self.ratings = Ratings(games, cutoff, cfg) if games else None
        except (ValueError, np.linalg.LinAlgError):
            self.ratings = None
            self.fit_error = "Scoring fit failed numerical checks"
            return
        fits = {}
        for g in games:
            before = week_cutoff(date(g["kickoff"]))
            key = (g["week"], before)
            if key not in fits:
                prior = history(games, g, before)
                try:
                    fits[key] = Ratings(prior, before, cfg) if prior else None
                except (ValueError, np.linalg.LinAlgError):
                    fits[key] = None
            fit = fits[key]
            if fit is None or min(fit.counts.get(g[t], 0) for t in ("home", "away")) < cfg.residual_warmup_team_games:
                continue
            means = fit.means(g)
            if not np.all(np.isfinite(means) & (means > 0)):
                continue
            self.bank.append(dict(game_id=g["game_id"], kickoff=g["kickoff"], as_of=before.isoformat(),
                                  released_at=g["released_at"], actual=[g["home_score"], g["away_score"]],
                                  predicted=means.tolist()))
        self.bank_weights = weights(self.bank, cutoff, cfg)

    def forecast(self, g, origin):
        cfg, fit = self.cfg, self.ratings
        counts = {g[t]: fit.counts.get(g[t], 0) if fit else 0 for t in ("home", "away")}
        result = dict(model_id=MODEL_ID, model_version=VERSION, **{k: g[k] for k in TARGET_FIELDS},
                      as_of=self.cutoff.isoformat(), data_snapshot_hash=self.hash, config_hash=digest(asdict(cfg)),
                      forecast_origin=origin, input_game_ids=[x["game_id"] for x in self.games],
                      team_game_counts=counts, residual_pair_count=len(self.bank),
                      residual_effective_n=effective_n(self.bank_weights), quality_flags=[])
        if g["game_type"] != "REG":
            reason = "Regular-season games only"
        elif g["neutral_site"] is None or not g.get("kickoff_confirmed", True):
            reason = "Awaiting confirmed kickoff or venue"
        elif self.fit_error:
            reason = self.fit_error
        elif (min(counts.values()) < cfg.min_team_games or len(self.bank) < cfg.min_residual_pairs
              or result["residual_effective_n"] < cfg.min_residual_effective_n):
            reason = "Insufficient current-season data"
        else:
            means = fit.means(g)
            if np.all(np.isfinite(means) & (means > 0)):
                seed = int(digest([VERSION, result["config_hash"], self.hash, result["as_of"],
                                   {k: g[k] for k in TARGET_FIELDS}])[:16], 16)
                h, a = (fit.summary(g[t]) for t in ("home", "away"))
                result.update(status="available", unavailable_reason=None, limited_sample=min(counts.values()) <= 5,
                              teams={g["home"]: h, g["away"]: a}, neutral_baseline=fit.coef[0], home_advantage=fit.coef[1],
                              baseline_means=[(h["weighted_for"]+a["weighted_allowed"])/2,
                                              (a["weighted_for"]+h["weighted_allowed"])/2],
                              fitted_score_means=means.tolist(), random_seed=str(seed), simulation_count=cfg.simulations)
                errors = np.array([np.subtract(p["actual"], p["predicted"]) for p in self.bank])
                result.update(simulate(means, errors, self.bank_weights, g["neutral_site"], cfg.simulations, seed))
                return result
            reason = "Invalid fitted score means"
        return {**result, "status": "unavailable", "unavailable_reason": reason}


def distribution(values, counts):
    bins, ids = np.unique(values, return_inverse=True)
    freq = np.bincount(ids, weights=counts).astype(int)
    bins, freq = bins[freq > 0], freq[freq > 0]
    cum = np.cumsum(freq)
    q = bins[np.searchsorted(cum, np.array([.1, .25, .5, .75, .9]) * cum[-1])].tolist()
    return dict(mean=float(np.average(bins, weights=freq)), median=q[2], interval_50=q[1:4:2],
                interval_80=q[::4], histogram=np.column_stack((bins, freq)).tolist())


def simulate(means, errors, weights, neutral, n, seed):
    p = weights / weights.sum()
    errors = errors - np.average(errors, axis=0, weights=p)
    if neutral:
        errors = np.concatenate((errors, errors[:, ::-1]))
        p = np.tile(p / 2, 2)
    rounded = np.floor(means + errors + .5)
    scenarios = np.maximum(0, rounded).astype(int)
    # Accumulate repeats of each paired scenario. All statistics use these same draws.
    ids = np.random.default_rng(seed).choice(len(p), n, p=p)
    counts = np.bincount(ids, minlength=len(p))
    h, a = scenarios.T
    margin = h - a
    probs = [float(counts[m].sum()/n) for m in (margin > 0, margin < 0, margin == 0)]
    exact = [float(p[m].sum()) for m in (margin > 0, margin < 0, margin == 0)]
    dist = {k: distribution(v, counts) for k, v in (("home", h), ("away", a), ("total", h+a), ("margin", margin))}
    shifts = [dist[k]["mean"] - m for k, m in zip(("home", "away"), means)]
    clip_rate = np.average((rounded < 0).any(axis=1), weights=counts)
    flags = []
    if any(x in (0, 1) for x in probs):
        flags.append("Boundary outcome probability")
    if clip_rate > .05 or max(abs(x) for x in shifts) > 1 or probs[2] > .05:
        flags.append("Review clipping, score shift or tie frequency")
    pairs, inverse = np.unique(scenarios, axis=0, return_inverse=True)
    freq = np.bincount(inverse.ravel(), weights=counts).astype(int)
    joint = np.column_stack((pairs[freq>0], freq[freq>0])).tolist()
    return dict(distributions=dist, paired_score_counts=joint, probability_home_win=probs[0], probability_away_win=probs[1],
                probability_tie=probs[2], home_win_given_no_tie=probs[0]/(1-probs[2]) if probs[2]<1 else None,
                diagnostics=dict(clipped_draw_rate=clip_rate, mean_shifts=shifts, exact_outcome_probabilities=exact,
                                 max_sampling_difference=max(abs(x-y) for x,y in zip(probs, exact))),
                quality_flags=flags)


def total_probabilities(forecast, line):
    if forecast.get("status") != "available" or not math.isfinite(line) or line % 1 != .5:
        return None
    over = sum(count for score, count in forecast["distributions"]["total"]["histogram"] if score > line)
    p = over / forecast["simulation_count"]
    return {"over": p, "under": 1-p, "boundary": p in (0, 1)}
