"""Transactional pregame snapshots. Historical replays never write to this archive."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sqlite3

import polars as pl

from .data import DATA, schedule_rows, schedules
from .monte_carlo import Config, MODEL_ID, VERSION, TARGET_FIELDS, Snapshot, completed_inputs, date, digest, history

DB = DATA / "monte_carlo.sqlite3"


def season_rows(season):
    return schedule_rows(schedules().filter(pl.col("season") == season))


def unavailable(g, reason):
    return {**{k: g[k] for k in TARGET_FIELDS}, "model_id": MODEL_ID, "model_version": VERSION,
            "status": "unavailable", "unavailable_reason": reason, "forecast_origin": None}


def _open(path):
    db = sqlite3.connect(path, timeout=60)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS observed (game TEXT PRIMARY KEY, revision TEXT, first_seen TEXT);
        CREATE TABLE IF NOT EXISTS snapshots (hash TEXT PRIMARY KEY, inputs TEXT);
        CREATE TABLE IF NOT EXISTS banks (snapshot TEXT, cutoff TEXT, config TEXT, version TEXT, pairs TEXT,
            PRIMARY KEY (snapshot, cutoff, config, version));
        CREATE TABLE IF NOT EXISTS artifacts (id INTEGER PRIMARY KEY, game TEXT, identity TEXT,
            generated TEXT, payload TEXT);
        CREATE INDEX IF NOT EXISTS by_game ON artifacts(game,id);
        CREATE TABLE IF NOT EXISTS frozen (game TEXT PRIMARY KEY, artifact INTEGER);
    """)
    return db


def _observe(db, games, now):
    seen = {gid: (revision, at) for gid, revision, at in db.execute("SELECT * FROM observed")}
    eligible = []
    for game in games:
        if date(game["released_at"]) > now:
            continue
        gid, revision = game["game_id"], digest(game)
        old = seen.get(gid)
        if not old or old[0] != revision:
            first_seen = now.isoformat()
            db.execute("INSERT OR REPLACE INTO observed VALUES (?,?,?)", (gid, revision, first_seen))
        else:
            first_seen = old[1]
        eligible.append({**game, "observed_at": first_seen})
    return eligible


def _freeze(db, game):
    gid = game["game_id"]
    db.execute("""INSERT OR IGNORE INTO frozen VALUES (?, (SELECT id FROM artifacts
                  WHERE game=? AND generated<? ORDER BY id DESC LIMIT 1))""", (gid, gid, game["kickoff"]))


def _frozen(db, gid):
    return db.execute("""SELECT a.payload FROM frozen f LEFT JOIN artifacts a ON a.id=f.artifact
                         WHERE f.game=?""", (gid,)).fetchone()


def forecasts(rows, season, selected, *, path=DB, config=None, clock=lambda: datetime.now(timezone.utc),
              source_updated_at=None):
    cfg = config or Config.from_env()
    config_hash = digest(asdict(cfg))
    results, snapshots = {}, {}
    games = completed_inputs(rows, season)
    with closing(_open(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = clock()
        games = _observe(db, games, now)
        for game in rows:
            final_result = (game.get("home_score") is not None and game.get("away_score") is not None
                            and game.get("result") == game["home_score"] - game["away_score"])
            if game["season"] == season and ((game.get("kickoff_confirmed", True) and date(game["kickoff"]) <= now) or
                    final_result or
                    str(game.get("source_status") or "").lower() in {"live", "in progress", "final", "final/ot", "completed"}):
                _freeze(db, game)
        for g in selected:
            gid = g["game_id"]
            if g["season"] != season or g["game_type"] != "REG":
                results[gid] = unavailable(g, "Regular-season games only")
                continue
            frozen = _frozen(db, gid)
            if frozen is not None:
                results[gid] = json.loads(frozen[0]) if frozen[0] else unavailable(g, "No archived pregame MC prediction")
                continue
            eligible = history(games, g, now, live=True)
            data_hash = digest(eligible)
            identity = digest([data_hash, config_hash, VERSION, {k: g[k] for k in TARGET_FIELDS},
                               g.get("kickoff_confirmed", True)])
            last = db.execute("SELECT identity,payload FROM artifacts WHERE game=? ORDER BY id DESC LIMIT 1",
                              (gid,)).fetchone()
            if last and last[0] == identity:
                results[gid] = json.loads(last[1])
                continue
            if data_hash not in snapshots:
                snap = Snapshot(eligible, now, cfg)
                snapshots[data_hash] = snap
                db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (data_hash, json.dumps(eligible)))
                db.execute("INSERT OR IGNORE INTO banks VALUES (?,?,?,?,?)",
                           (data_hash, now.isoformat(), config_hash, VERSION, json.dumps(snap.bank)))
            forecast = snapshots[data_hash].forecast(g, "archived")
            generated = clock()
            if generated >= date(g["kickoff"]):
                _freeze(db, g)
                frozen = _frozen(db, gid)
                results[gid] = json.loads(frozen[0]) if frozen[0] else unavailable(g, "No archived pregame MC prediction")
                continue
            forecast.update(generated_at=generated.isoformat(), source_updated_at=source_updated_at,
                            config=asdict(cfg), residual_origin="reconstruction")
            db.execute("INSERT INTO artifacts(game,identity,generated,payload) VALUES (?,?,?,?)",
                       (gid, identity, forecast["generated_at"], json.dumps(forecast, allow_nan=False)))
            results[gid] = forecast
    return results


def get_forecasts(season, selected):
    rows = season_rows(season)
    by_id = {g["game_id"]: g for g in rows}
    source = DATA / "schedules.parquet"
    updated = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat() if source.exists() else None
    current = [by_id[g["game_id"]] for g in selected if g["game_id"] in by_id]
    result = forecasts(rows, season, current, source_updated_at=updated)
    for g in selected:
        if g["game_id"] not in result:
            result[g["game_id"]] = unavailable(g, "Game missing from the latest schedule")
    return result
