"""Pregame player-yardage forecasts from NFLverse weekly box scores."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from statistics import mean
from tempfile import NamedTemporaryFile
from threading import Lock
import time
import unicodedata

import nflreadpy as nfl
import numpy as np
import polars as pl

from .data import DATA, _fresh, canonical
from .robust import winsorize

STATS = {"rushing": ("rushing_yards", "carries"), "passing": ("passing_yards", "attempts")}
POSITIONS = {"QB", "RB", "FB"}
MODEL_VERSION = "player-yardage-v1"
METHOD = "Robust yardage and workload with opponent adjustment; 80% prediction interval from prior-season errors."
PLAYER_COLUMNS = ("game_id", "player_id", "team", "position", "opponent_team",
                  "passing_yards", "attempts", "rushing_yards", "carries")
TEAM_COLUMNS = ("game_id", "team", "opponent_team", "passing_yards", "rushing_yards")
ROSTER_COLUMNS = ("gsis_id", "full_name", "position", "status", "team", "week")
_MIN_HISTORY = 3
_MIN_ERRORS = 60
_STAT_SCALE = {"rushing": 8.0, "passing": 22.0}
_VOLUME_SCALE = {"rushing": 2.0, "passing": 5.0}
_LEAGUE_YPA = {"rushing": 4.3, "passing": 7.0}
_MIN_PICK_VOLUME = {"rushing": 4.0, "passing": 15.0}
_INACTIVE_GAMES = {"cancelled", "canceled", "postponed", "suspended"}


def name_key(value: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return "".join(ch for ch in ascii_name.casefold() if ch.isalnum())


def kickoff(game: dict) -> datetime:
    return datetime.fromisoformat(game["kickoff"])


def scheduled(game: dict) -> bool:
    return bool(game.get("kickoff_confirmed")) and str(game.get("source_status") or "").strip().lower() not in _INACTIVE_GAMES


def is_pregame(game: dict, now: datetime) -> bool:
    status = str(game.get("source_status") or "").strip().lower()
    return (scheduled(game) and kickoff(game) > now and game["home_score"] is None
            and game["away_score"] is None and status not in {"live", "in progress", "final", "final/ot", "completed"})


def final_at(game: dict) -> datetime:
    """Reject same-window games and conservatively allow time for the final to publish."""
    known = game.get("result_available_at")
    if known:
        try:
            stamp = datetime.fromisoformat(known)
            if stamp.tzinfo is not None and stamp >= kickoff(game):
                return stamp
        except (TypeError, ValueError):
            pass
    return kickoff(game) + timedelta(hours=4)


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    team: str
    position: str
    roster_week: int


@dataclass(frozen=True)
class Sample:
    season: int
    week: int
    kickoff: datetime
    available: datetime
    opponent: str
    yards: float
    volume: float


@dataclass(frozen=True)
class DefenseSample:
    season: int
    available: datetime
    yards: float


@dataclass(frozen=True)
class Estimate:
    value: float
    volume: float
    defense: float
    history: list[Sample]
    robust_yards: np.ndarray
    opponent_games: int


def valid_stat(position: str, stat: str) -> bool:
    return (stat == "passing" and position == "QB") or (stat == "rushing" and position in POSITIONS)


def role(position: str) -> str:
    return "qb" if position == "QB" else "back"


def _weighted(values, samples, season):
    weights = [1.0 if sample.season == season else 0.35 for sample in samples]
    return float(np.average(values, weights=weights))


def _spread(stat, value):
    return (40.0 if stat == "passing" else 12.0) + 0.28 * abs(value)


class YardageData:
    def __init__(self, season, games, player_rows, team_rows, roster_rows):
        self.season = season
        self.games = {game["game_id"]: game for game in games}
        completed = {
            game_id: (game, kickoff(game), final_at(game)) for game_id, game in self.games.items()
            if scheduled(game) and game["home_score"] is not None and game["away_score"] is not None
        }
        self.players = defaultdict(list)
        self.defense = defaultdict(list)
        self.roster = defaultdict(list)
        for row in roster_rows:
            team = canonical(row["team"])
            if row["gsis_id"] and row["full_name"] and team and row["position"] in POSITIONS:
                # Keep inactive rows so roster snapshots never revive an inactive player.
                player = Player(row["gsis_id"], row["full_name"], team, row["position"], row["week"])
                self.roster[team].append((player, row["status"]))

        seen = set()
        self.replay = []
        for row in player_rows:
            game_id, player_id = row["game_id"], row["player_id"]
            position, opponent = row["position"], canonical(row["opponent_team"])
            if not player_id or position not in POSITIONS or not opponent or game_id not in completed:
                continue
            game, at, ready = completed[game_id]
            if not _teams_match(game, row["team"], opponent) or (game_id, player_id) in seen:
                continue
            seen.add((game_id, player_id))
            for stat, (yard_field, volume_field) in STATS.items():
                yards, volume = row[yard_field], row[volume_field]
                if not valid_stat(position, stat) or not _valid_sample(yards, volume):
                    continue
                sample = Sample(game["season"], game["week"], at, ready, opponent, yards, volume)
                self.players[(player_id, stat)].append(sample)
                if game["season"] == season - 1:
                    self.replay.append((player_id, position, stat, game, sample))

        seen = set()
        for row in team_rows:
            game_id, opponent = row["game_id"], canonical(row["opponent_team"])
            if not opponent or game_id not in completed or (game_id, opponent) in seen:
                continue
            game, _, ready = completed[game_id]
            if not _teams_match(game, row["team"], opponent):
                continue
            seen.add((game_id, opponent))
            for stat, (yard_field, _) in STATS.items():
                yards = row[yard_field]
                if yards is not None and np.isfinite(yards):
                    sample = DefenseSample(game["season"], ready, yards)
                    self.defense[(opponent, stat)].append(sample)
        for observations in self.players.values():
            observations.sort(key=lambda x: x.kickoff)
        for observations in self.defense.values():
            observations.sort(key=lambda x: x.available)

    def active_players(self, teams, week):
        result = []
        for team in teams:
            eligible = [(player, status) for player, status in self.roster[team]
                        if player.roster_week <= week]
            if not eligible:
                continue
            latest = max(player.roster_week for player, _ in eligible)
            if week - latest > 2:
                continue
            unique = {}
            for player, status in eligible:
                if player.roster_week == latest:
                    unique.setdefault(player.id, []).append((player, status))
            for entries in unique.values():
                if all(status == "ACT" for _, status in entries):
                    result.append(entries[0][0])
        return result

    def estimate(self, player_id, stat, game, opponent, as_of):
        history = [sample for sample in self.players.get((player_id, stat), ())
                   if sample.available <= as_of][-20:]
        if len(history) < _MIN_HISTORY or not any(sample.volume > 0 for sample in history):
            return None
        season = game["season"]
        yards = np.array([sample.yards for sample in history])
        volume = np.array([sample.volume for sample in history])
        clipped_yards = winsorize(yards, min_scale=_STAT_SCALE[stat], min_samples=5)
        clipped_volume = winsorize(volume, min_scale=_VOLUME_SCALE[stat], min_samples=5, lower=0.0)
        base_yards = _weighted(clipped_yards, history, season)
        rec_yards = _weighted(clipped_yards[-5:], history[-5:], season)
        expected_volume = 0.65 * _weighted(clipped_volume, history, season)
        expected_volume += 0.35 * _weighted(clipped_volume[-5:], history[-5:], season)
        # Use a small position-agnostic efficiency prior, supported by the volume sample.
        prior_plays = 4.0 if stat == "rushing" else 10.0
        efficiency = (float(clipped_yards.sum()) + prior_plays * _LEAGUE_YPA[stat])
        efficiency /= float(clipped_volume.sum()) + prior_plays
        player_yards = 0.75 * (0.65 * base_yards + 0.35 * rec_yards) + 0.25 * expected_volume * efficiency

        league, allowed, opp_games = self.allowance(stat, opponent, season, as_of)
        if opp_games < 3 or league <= 0:
            return None
        # The league-relative defense adjustment is shrunk and bounded, and does
        # not boost a low-use player into a starter's projected workload.
        shrunk_defense = (allowed * opp_games + league * 6.0) / (opp_games + 6.0)
        multiplier = 1.0 + float(np.clip(0.5 * (shrunk_defense / league - 1.0), -0.15, 0.15))
        return Estimate(player_yards * multiplier, expected_volume, allowed, history, clipped_yards, opp_games)

    @cached_property
    def league_series(self):
        return {
            stat: sorted([sample for (_, kind), history in self.defense.items() if kind == stat
                          for sample in history], key=lambda sample: sample.available)
            for stat in STATS
        }

    def allowance(self, stat, opponent, season, as_of):
        league_games = self.league_series[stat]
        end = bisect_right(league_games, as_of, key=lambda sample: sample.available)
        # Use up to 320 team observations (about ten NFL weeks) for the league reference.
        league_games = league_games[max(0, end - 320):end]
        opponent_games = [sample for sample in self.defense.get((opponent, stat), ())
                          if sample.available <= as_of][-16:]
        if not league_games or not opponent_games:
            return 0.0, 0.0, 0
        reference = winsorize([sample.yards for sample in league_games], min_scale=_STAT_SCALE[stat])
        yards = winsorize([sample.yards for sample in opponent_games], min_scale=_STAT_SCALE[stat])
        return float(reference.mean()), _weighted(yards, opponent_games, season), len(opponent_games)

    @cached_property
    def residuals(self):
        errors = defaultdict(list)
        for player_id, position, stat, game, sample in self.replay:
            estimate = self.estimate(player_id, stat, game, sample.opponent, sample.kickoff)
            if estimate is not None:
                residual = (sample.yards - estimate.value) / _spread(stat, estimate.value)
                errors[(stat, role(position))].append(residual)
        return {key: np.array(values) for key, values in errors.items()}

    def project(self, player, stat, game, as_of=None):
        opponent = game["home"] if player.team == game["away"] else game["away"]
        at = kickoff(game)
        cutoff = min(as_of or datetime.now(timezone.utc), at)
        estimate = self.estimate(player.id, stat, game, opponent, cutoff)
        residuals = self.residuals.get((stat, role(player.position)))
        if estimate is None or residuals is None or len(residuals) < _MIN_ERRORS:
            return None

        yards = estimate.value + _spread(stat, estimate.value) * residuals
        low, high = np.quantile(yards, [0.10, 0.90])
        season_history = [sample.yards for sample in estimate.history if sample.season == game["season"]]
        raw_average = mean(season_history) if season_history else None
        robust_average = winsorize(season_history, min_scale=_STAT_SCALE[stat], min_samples=5)
        return Projection({
            "model_version": MODEL_VERSION,
            "method": METHOD,
            "player": player.name,
            "player_id": player.id,
            "team": player.team,
            "opponent": opponent,
            "position": player.position,
            "stat": stat,
            "season": game["season"],
            "week": game["week"],
            "game_id": game["game_id"],
            "kickoff": game["kickoff"],
            "roster_week": player.roster_week,
            "projection": round(estimate.value, 1),
            "interval_low": round(float(low), 1),
            "interval_high": round(float(high), 1),
            "interval_coverage": 0.80,
            "season_average": round(raw_average, 1) if raw_average is not None else None,
            "robust_season_average": round(float(robust_average.mean()), 1) if season_history else None,
            "opponent_allowed_average": round(estimate.defense, 1),
            "opponent_games_sampled": estimate.opponent_games,
            "expected_attempts": round(estimate.volume, 1),
            "games_sampled": len(estimate.history),
            "residual_games": len(residuals),
            "history": [
                {"season": sample.season, "week": sample.week, "kickoff": sample.kickoff.isoformat(),
                 "opponent": sample.opponent, "yards": sample.yards, "attempts": sample.volume,
                 "robust_yards": round(float(robust), 1), "outlier": not np.isclose(sample.yards, robust).item()}
                for sample, robust in zip(estimate.history, estimate.robust_yards)
            ],
        }, yards, estimate.volume)


@dataclass(frozen=True)
class Projection:
    payload: dict
    outcomes: np.ndarray
    volume: float

    def priceable(self) -> bool:
        stat = self.payload["stat"]
        return self.volume >= _MIN_PICK_VOLUME[stat] and self.payload["games_sampled"] >= 5

    def over_probability(self, line: float) -> float:
        # Actual yardage is integer; floor(x + 0.5) preserves the exact half-point threshold.
        return float((np.count_nonzero(self.outcomes >= line) + 0.5) / (len(self.outcomes) + 1))


def _teams_match(game, team, opponent):
    return canonical(team) != opponent and {canonical(team), opponent} == {game["away"], game["home"]}


def _valid_sample(yards, volume):
    return (yards is not None and volume is not None and np.isfinite(yards)
            and np.isfinite(volume) and volume >= 0 and (volume > 0 or yards == 0))


_source_lock = Lock()
_data_cache = {}
_LOADERS = {
    "stats_player": (nfl.load_player_stats, PLAYER_COLUMNS, "stats_player_week"),
    "stats_team": (nfl.load_team_stats, TEAM_COLUMNS, "stats_team_week"),
    "roster_weekly": (nfl.load_rosters_weekly, ROSTER_COLUMNS, "roster_weekly"),
}


def _cached_rows(source, season, age):
    load, columns, cache_key = _LOADERS[source]
    path = DATA / "player_yardage" / f"{source}_{season}.parquet"
    if not _fresh(path, age):
        tmp = None
        try:
            nfl.clear_cache(f"{cache_key}_{season}")
            frame = load(season).select(columns)
            if frame.is_empty():
                raise ValueError("empty NFLverse player-yardage data")
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent, delete=False) as file:
                tmp = Path(file.name)
                frame.write_parquet(file)
            tmp.replace(path)
        except Exception as exc:
            if not path.exists():
                raise RuntimeError(f"NFLverse {source} data is unavailable for {season}.") from exc
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    return pl.read_parquet(path, columns=columns).iter_rows(named=True)


def yardage_data(season, calendar):
    """Refresh on demand; share one data snapshot across simultaneous requests."""
    with _source_lock:
        cached = _data_cache.get(season)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        games = [game for game in calendar if season - 2 <= game["season"] <= season]
        current_finals = any(game["season"] == season and game["home_score"] is not None
                             and game["away_score"] is not None for game in games)
        player_rows, team_rows = [], []
        for year in range(season - 2, season + 1):
            if year == season and not current_finals:
                continue
            age = 1 if year == season else 24 * 30
            player_rows.extend(_cached_rows("stats_player", year, age))
            team_rows.extend(_cached_rows("stats_team", year, age))
        roster = list(_cached_rows("roster_weekly", season, 1))
        result = YardageData(season, games, player_rows, team_rows, roster)
        # Finish calibration before sharing the snapshot with request threads.
        _ = result.residuals
        if len(_data_cache) >= 3:
            _data_cache.pop(next(iter(_data_cache)))
        _data_cache[season] = (time.monotonic(), result)
        return result
