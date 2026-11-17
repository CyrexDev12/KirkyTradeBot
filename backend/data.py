"""NFLverse ingestion and leak-free pregame feature construction."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
import json
import math
import os

import nflreadpy as nfl
import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("NFL_DATA_DIR", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
START_SEASON = int(os.getenv("NFL_START_SEASON", "2015"))
MODEL_VERSION = "epa-elo-hgb-v2"
FEATURE_PATH = DATA / "features_totals_v1.json"

TEAMS = {
    "ARI": ("Arizona Cardinals", "#97233F", "ari"), "ATL": ("Atlanta Falcons", "#A71930", "atl"),
    "BAL": ("Baltimore Ravens", "#241773", "bal"), "BUF": ("Buffalo Bills", "#00338D", "buf"),
    "CAR": ("Carolina Panthers", "#0085CA", "car"), "CHI": ("Chicago Bears", "#0B162A", "chi"),
    "CIN": ("Cincinnati Bengals", "#FB4F14", "cin"), "CLE": ("Cleveland Browns", "#FF3C00", "cle"),
    "DAL": ("Dallas Cowboys", "#003594", "dal"), "DEN": ("Denver Broncos", "#FB4F14", "den"),
    "DET": ("Detroit Lions", "#0076B6", "det"), "GB": ("Green Bay Packers", "#203731", "gb"),
    "HOU": ("Houston Texans", "#03202F", "hou"), "IND": ("Indianapolis Colts", "#002C5F", "ind"),
    "JAX": ("Jacksonville Jaguars", "#006778", "jax"), "KC": ("Kansas City Chiefs", "#E31837", "kc"),
    "LA": ("Los Angeles Rams", "#003594", "lar"), "LAC": ("Los Angeles Chargers", "#0080C6", "lac"),
    "LV": ("Las Vegas Raiders", "#A5ACAF", "lv"), "MIA": ("Miami Dolphins", "#008E97", "mia"),
    "MIN": ("Minnesota Vikings", "#4F2683", "min"), "NE": ("New England Patriots", "#002244", "ne"),
    "NO": ("New Orleans Saints", "#D3BC8D", "no"), "NYG": ("New York Giants", "#0B2265", "nyg"),
    "NYJ": ("New York Jets", "#125740", "nyj"), "PHI": ("Philadelphia Eagles", "#004C54", "phi"),
    "PIT": ("Pittsburgh Steelers", "#FFB612", "pit"), "SEA": ("Seattle Seahawks", "#69BE28", "sea"),
    "SF": ("San Francisco 49ers", "#AA0000", "sf"), "TB": ("Tampa Bay Buccaneers", "#D50A0A", "tb"),
    "TEN": ("Tennessee Titans", "#0C2340", "ten"), "WAS": ("Washington Commanders", "#5A1414", "was"),
}
ALIASES = {"STL": "LA", "SD": "LAC", "OAK": "LV", "JAC": "JAX", "WSH": "WAS", "LAR": "LA"}

METRICS = ["off_epa", "off_success", "pass_epa", "rush_epa", "explosive", "def_epa", "def_success", "def_pass_epa", "def_rush_epa", "def_explosive", "qb_epa", "cpoe"]
FEATURES = ["elo_diff", "home_rest_diff", "home_win_pct_diff", "recent_win_pct_diff", "sos_diff", "off_epa_diff", "def_epa_diff", "off_success_diff", "def_success_diff", "pass_epa_diff", "rush_epa_diff", "def_pass_epa_diff", "def_rush_epa_diff", "explosive_diff", "def_explosive_diff", "qb_epa_diff", "cpoe_diff", "recent_off_epa_diff", "recent_def_epa_diff", "experience_min"]
TOTAL_FEATURES = ["season", "week", "home_rest", "away_rest", "home_off_epa", "away_off_epa", "home_def_epa", "away_def_epa", "home_pass_epa", "away_pass_epa", "home_def_pass_epa", "away_def_pass_epa", "home_off_success", "away_off_success", "home_explosive", "away_explosive", "home_qb_epa", "away_qb_epa", "home_points_for", "away_points_for", "home_points_against", "away_points_against", "home_recent_points_for", "away_recent_points_for", "home_recent_points_against", "away_recent_points_against", "home_plays", "away_plays", "home_games", "away_games"]


def canonical(team: str | None) -> str | None:
    if team is None:
        return None
    return ALIASES.get(team.upper(), team.upper())


def team_info(abbr: str) -> dict:
    name, color, espn = TEAMS.get(abbr, (abbr, "#7D8AA3", abbr.lower()))
    return {"abbr": abbr, "name": name, "color": color, "logo": f"https://a.espncdn.com/i/teamlogos/nfl/500/{espn}.png"}


def _fresh(path: Path, max_age_hours: int) -> bool:
    return path.exists() and (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime < max_age_hours * 3600)


def schedules(refresh: bool = False) -> pl.DataFrame:
    path = DATA / "schedules.parquet"
    if refresh or not _fresh(path, 6):
        try:
            df = nfl.load_schedules(seasons=True)
            df.write_parquet(path)
        except Exception:
            if not path.exists():
                raise
    return pl.read_parquet(path)


def season_pbp(season: int, refresh: bool = False) -> pl.DataFrame:
    path = DATA / f"team_games_{season}.parquet"
    age = 6 if season >= datetime.now().year else 24 * 365
    if refresh or not _fresh(path, age):
        try:
            pbp = nfl.load_pbp(season)
            # nflverse can update a current game during play. Only games with a
            # final schedule result enter the training/pregame feature history.
            cols = ["game_id", "posteam", "defteam", "epa", "success", "pass", "rush", "yards_gained", "qb_dropback", "cpoe", "play_type"]
            pbp = pbp.select([c for c in cols if c in pbp.columns]).filter(
                pl.col("posteam").is_not_null() & pl.col("defteam").is_not_null()
                & pl.col("epa").is_not_null() & pl.col("play_type").is_in(["pass", "run"])
            )
            pbp = pbp.with_columns([
                (pl.col("yards_gained") >= pl.when(pl.col("pass") == 1).then(20).otherwise(10)).cast(pl.Float64).alias("explosive_flag"),
                pl.when(pl.col("qb_dropback") == 1).then(pl.col("epa")).otherwise(None).alias("qb_play_epa"),
            ])
            off = pbp.group_by(["game_id", "posteam"]).agg([
                pl.len().alias("plays"), pl.col("epa").mean().alias("off_epa"), pl.col("success").mean().alias("off_success"),
                pl.col("epa").filter(pl.col("pass") == 1).mean().alias("pass_epa"),
                pl.col("epa").filter(pl.col("rush") == 1).mean().alias("rush_epa"),
                pl.col("explosive_flag").mean().alias("explosive"), pl.col("qb_play_epa").mean().alias("qb_epa"),
                pl.col("cpoe").mean().alias("cpoe"),
            ]).rename({"posteam": "team"})
            defense = pbp.group_by(["game_id", "defteam"]).agg([
                pl.col("epa").mean().alias("def_epa"), pl.col("success").mean().alias("def_success"),
                pl.col("epa").filter(pl.col("pass") == 1).mean().alias("def_pass_epa"),
                pl.col("epa").filter(pl.col("rush") == 1).mean().alias("def_rush_epa"),
                pl.col("explosive_flag").mean().alias("def_explosive"),
            ]).rename({"defteam": "team"})
            off.join(defense, on=["game_id", "team"], how="left").write_parquet(path)
        except Exception:
            if not path.exists():
                raise
    return pl.read_parquet(path)


def _kickoff(row: dict) -> datetime | None:
    try:
        day = row["gameday"]
        clock = row.get("gametime") or "12:00"
        # nflverse schedules document gametime in US/Eastern.
        from zoneinfo import ZoneInfo
        return datetime.combine(datetime.fromisoformat(day).date(), time.fromisoformat(clock), ZoneInfo("America/New_York")).astimezone(timezone.utc)
    except Exception:
        return None


def schedule_rows(df: pl.DataFrame) -> list[dict]:
    rows = []
    for r in df.iter_rows(named=True):
        if r.get("game_type") not in ("REG", "WC", "DIV", "CON", "SB"):
            continue
        kick = _kickoff(r)
        if kick is None:
            continue
        home, away = canonical(r["home_team"]), canonical(r["away_team"])
        if home not in TEAMS or away not in TEAMS:
            continue
        rows.append({"game_id": r["game_id"], "season": r["season"], "week": r["week"], "game_type": r["game_type"],
                     "kickoff": kick.isoformat(), "home": home, "away": away, "home_score": r["home_score"],
                     "away_score": r["away_score"], "home_rest": r.get("home_rest"), "away_rest": r.get("away_rest")})
    return sorted(rows, key=lambda r: (r["kickoff"], r["game_id"]))


def _mean(games: list[dict], metric: str, season: int, recent: bool = False) -> float:
    if not games:
        return 0.0
    selected = games[-6:] if recent else games[-24:]
    values, weights = [], []
    for g in selected:
        x = g.get(metric)
        if x is None or not math.isfinite(float(x)):
            continue
        values.append(float(x))
        weights.append(1.0 if g["season"] == season else 0.35)
    return float(np.average(values, weights=weights)) if values else 0.0


def build_features(rows: list[dict], team_games: pl.DataFrame) -> list[dict]:
    """Every feature snapshot is taken before the current game's result is applied."""
    stats = {(r["game_id"], canonical(r["team"])): r for r in team_games.iter_rows(named=True)}
    history: dict[str, list[dict]] = defaultdict(list)
    elo: dict[str, float] = defaultdict(lambda: 1500.0)
    last_season = None
    out = []
    for game in rows:
        season, home, away = game["season"], game["home"], game["away"]
        if last_season is not None and season != last_season:
            for team in list(elo):
                elo[team] = 1500 + (elo[team] - 1500) * 0.72
        last_season = season
        h, a = history[home], history[away]
        hs = [x for x in h if x["season"] == season]
        ass = [x for x in a if x["season"] == season]
        def winpct(seq):
            return float(np.mean([x["win"] for x in seq])) if seq else 0.5
        hm = {m: _mean(h, m, season) for m in METRICS}
        am = {m: _mean(a, m, season) for m in METRICS}
        hr = {m: _mean(h, m, season, True) for m in ("off_epa", "def_epa")}
        ar = {m: _mean(a, m, season, True) for m in ("off_epa", "def_epa")}
        feats = {
            "elo_diff": (elo[home] + 55 - elo[away]) / 400,
            "home_rest_diff": max(-10, min(10, (game.get("home_rest") or 7) - (game.get("away_rest") or 7))),
            "home_win_pct_diff": winpct(hs) - winpct(ass),
            "recent_win_pct_diff": winpct(h[-6:]) - winpct(a[-6:]),
            "sos_diff": _mean(h, "opponent_elo", season) - _mean(a, "opponent_elo", season),
            "recent_off_epa_diff": hr["off_epa"] - ar["off_epa"],
            "recent_def_epa_diff": ar["def_epa"] - hr["def_epa"],
            "experience_min": min(len(h), len(a), 24) / 24,
        }
        for m in METRICS:
            feats[m + "_diff"] = (am[m] - hm[m]) if m.startswith("def_") else (hm[m] - am[m])
        total_feats = {"season": season, "week": game["week"],
                       "home_rest": game.get("home_rest") or 7, "away_rest": game.get("away_rest") or 7,
                       "home_games": min(len(h), 24), "away_games": min(len(a), 24)}
        for prefix, prior, means in (("home", h, hm), ("away", a, am)):
            for metric in ("off_epa", "def_epa", "pass_epa", "def_pass_epa", "off_success", "explosive", "qb_epa"):
                total_feats[f"{prefix}_{metric}"] = means[metric]
            for metric in ("points_for", "points_against", "plays"):
                total_feats[f"{prefix}_{metric}"] = _mean(prior, metric, season)
            for metric in ("points_for", "points_against"):
                total_feats[f"{prefix}_recent_{metric}"] = _mean(prior, metric, season, True)
        record = {**game, "features": {k: float(feats[k]) for k in FEATURES},
                  "total_features": {k: float(total_feats[k]) for k in TOTAL_FEATURES},
                  "home_metrics": {**hm, "elo": elo[home], "record": f'{sum(x["win"] == 1 for x in hs)}-{sum(x["win"] == 0 for x in hs)}'},
                  "away_metrics": {**am, "elo": elo[away], "record": f'{sum(x["win"] == 1 for x in ass)}-{sum(x["win"] == 0 for x in ass)}'}}
        out.append(record)
        if game["home_score"] is None or game["away_score"] is None:
            continue
        margin = game["home_score"] - game["away_score"]
        hwin = 1.0 if margin > 0 else (0.5 if margin == 0 else 0.0)
        expectation = 1 / (1 + 10 ** ((elo[away] - elo[home] - 55) / 400))
        update = 22 * math.log1p(abs(margin)) / math.log(15) * (hwin - expectation)
        pre_home, pre_away = elo[home], elo[away]
        elo[home] += update
        elo[away] -= update
        for team, win, opp_elo, scored, allowed in ((home, hwin, pre_away, game["home_score"], game["away_score"]), (away, 1-hwin, pre_home, game["away_score"], game["home_score"])):
            s = stats.get((game["game_id"], team), {})
            history[team].append({"season": season, "win": win, "opponent_elo": (opp_elo-1500)/400,
                                  "points_for": scored, "points_against": allowed, "plays": s.get("plays"),
                                  **{m: s.get(m) for m in METRICS}})
    return out


def load_feature_rows(refresh: bool = False) -> list[dict]:
    path = FEATURE_PATH
    # Refresh current-season features at most every 6 hours. Historical PBP
    # summaries remain on disk; no browser request downloads raw PBP.
    if not refresh and _fresh(path, 6):
        return json.loads(path.read_text())
    schedule = schedules(refresh=refresh)
    rows = schedule_rows(schedule.filter(pl.col("season") >= START_SEASON))
    seasons = sorted({r["season"] for r in rows if r["season"] <= datetime.now().year})
    all_stats = []
    errors = []
    for season in seasons:
        try:
            all_stats.append(season_pbp(season, refresh=refresh))
        except Exception as exc:
            errors.append(f"{season}: {exc}")
    if not all_stats:
        raise RuntimeError("No NFLverse play-by-play data available. " + "; ".join(errors))
    stats = pl.concat(all_stats, how="diagonal_relaxed")
    result = build_features(rows, stats)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, allow_nan=False))
    tmp.replace(path)
    (DATA / "data_status.json").write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "pbp_seasons": seasons, "errors": errors}))
    return result
