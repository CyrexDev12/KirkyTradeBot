"""Record and report read-only Oil D WebSocket shadow decisions."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from math import isfinite
from pathlib import Path
import sqlite3
import time

from .commodity import WTI


_DETAIL_FIELDS = (
    ("original_side", "paper side"), ("side", "side"), ("original_limit", "paper limit"),
    ("shadow_limit", "shadow limit"), ("best_ask", "ask"), ("original_best_ask", "ask for paper side"),
    ("ask_delta_cents", "ask gap ¢"), ("quantity", "quantity"), ("source", "source"),
)


def oil_d_shadow_path(directory: Path) -> Path:
    return directory / "shadow" / "oil-d.sqlite3"


def _encode(value):
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError(f"Cannot record a shadow field of type {type(value).__name__}")


class OilDShadowStore:
    """Persist one observation per signal and stage, keeping its newest evidence."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS oil_d_shadow (
                    slug TEXT NOT NULL,
                    signal_at REAL NOT NULL,
                    stage TEXT NOT NULL,
                    at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (slug, signal_at, stage)
                );
                CREATE INDEX IF NOT EXISTS oil_d_shadow_at ON oil_d_shadow(at);
                CREATE UNIQUE INDEX IF NOT EXISTS oil_d_shadow_filled_contract ON oil_d_shadow(slug)
                    WHERE stage='execution' AND json_extract(payload,'$.outcome')='filled';
            """)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=2)

    @staticmethod
    def _has_fill(db, slug: str) -> bool:
        return db.execute("""SELECT 1 FROM oil_d_shadow WHERE slug=? AND stage='execution'
                              AND json_extract(payload,'$.outcome')='filled'""", (slug,)).fetchone() is not None

    def has_fill(self, slug: str) -> bool:
        """Check the durable one-fill-per-market limit for the shadow simulation."""
        with closing(self._connect()) as db:
            return self._has_fill(db, slug)

    def record(self, slug: str, signal_at: float, stage: str, at: float, payload: dict) -> bool:
        """Record newer evidence and return whether the journal changed."""
        if not slug.strip() or not stage.strip() or not isfinite(signal_at) or not isfinite(at) or at < signal_at:
            raise ValueError("Shadow observations require a market, stage and chronological signal and event times")
        if not isinstance(payload, dict):
            raise TypeError("Shadow evidence must be a JSON object")
        encoded = json.dumps(payload, default=_encode, allow_nan=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            if stage == "execution" and payload.get("outcome") == "filled":
                db.execute("BEGIN IMMEDIATE")
                if self._has_fill(db, slug):
                    return False
            cursor = db.execute("""INSERT INTO oil_d_shadow(slug,signal_at,stage,at,payload) VALUES(?,?,?,?,?)
                ON CONFLICT(slug,signal_at,stage) DO UPDATE SET at=excluded.at,payload=excluded.payload
                WHERE excluded.at > oil_d_shadow.at AND (oil_d_shadow.stage!='execution'
                    OR json_extract(oil_d_shadow.payload,'$.outcome') IS NOT 'filled')""",
                (slug, signal_at, stage, at, encoded))
            return cursor.rowcount == 1


def report(path: Path, since: float, as_of: float, *, limit: int = 100) -> dict:
    """Count all observations in the period and return at most `limit` recent details."""
    if limit < 1 or not isfinite(since) or not isfinite(as_of) or since > as_of:
        raise ValueError("Expected a positive detail limit and chronological report times")
    result = {"since": since, "as_of": as_of, "markets": 0, "signals": 0, "observations": 0,
              "stages": {}, "recent": []}
    if not path.is_file():
        return result

    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='oil_d_shadow'").fetchone() is None:
            return result
        result["markets"], result["signals"] = db.execute("""SELECT count(DISTINCT slug), coalesce(sum(has_signal),0)
            FROM (SELECT slug,max(stage IN ('signal','execution')) AS has_signal FROM oil_d_shadow
            WHERE at BETWEEN ? AND ? GROUP BY slug,signal_at)""", (since, as_of)).fetchone()
        summaries = db.execute("""SELECT stage, json_extract(payload,'$.outcome') AS outcome,
            json_extract(payload,'$.reason') AS reason, count(*) FROM oil_d_shadow WHERE at BETWEEN ? AND ?
            GROUP BY stage,outcome,reason ORDER BY stage,outcome,reason""", (since, as_of))
        for stage, outcome, reason, count in summaries:
            metrics = result["stages"].setdefault(stage, {"observations": 0, "outcomes": {}, "reasons": {}})
            metrics["observations"] += count
            result["observations"] += count
            for key, value in (("outcomes", outcome or "observed"), ("reasons", reason)):
                if value is not None:
                    metrics[key][value] = metrics[key].get(value, 0) + count
        rows = db.execute("""SELECT slug,signal_at,stage,at,payload FROM oil_d_shadow WHERE at BETWEEN ? AND ?
            ORDER BY at DESC,slug,signal_at,stage LIMIT ?""", (since, as_of, limit))
        result["recent"] = [{"slug": slug, "signal_at": signal_at, "stage": stage, "at": at,
                             "payload": json.loads(payload)} for slug, signal_at, stage, at, payload in rows]
    return result


def _describe(counts):
    return ", ".join(f"{str(label).replace('_', ' ')} {count}" for label, count in counts.items())


def _paper_ask_gap(payload):
    if payload.get("ask_delta_cents") is not None:
        return payload["ask_delta_cents"]
    try:
        delta = (Decimal(str(payload.get("original_best_ask"))) - Decimal(str(payload.get("original_limit")))) * 100
    except InvalidOperation:
        return None
    return format(delta.normalize(), "f") if delta.is_finite() else None


def _print_report(result, hours):
    print(f"Oil D WebSocket shadow | {hours}h | markets {result['markets']} | signals {result['signals']} | "
          f"observations {result['observations']}")
    if not result["observations"]:
        print("No shadow observations were recorded for this period.")
        return
    for stage, data in result["stages"].items():
        print(f"{stage} | {_describe(data['outcomes'])}")
        if data["reasons"]:
            print(f"  {_describe(data['reasons'])}")
    for row in result["recent"]:
        payload = {**row["payload"], "ask_delta_cents": _paper_ask_gap(row["payload"])}
        stamp = datetime.fromtimestamp(row["at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        outcome = str(payload.get("outcome") or "observed").replace("_", " ")
        reason = str(payload.get("reason") or "").replace("_", " ")
        details = [f"{label} {payload[key]}" for key, label in _DETAIL_FIELDS if payload.get(key) is not None]
        for key, label in (("net_edge", "edge after fees"), ("original_net_edge", "paper-side edge after fees")):
            if payload.get(key) is not None:
                details.append(f"{label} {100 * float(payload[key]):+.2f}¢")
        fields = [stamp, row["slug"], row["stage"], outcome, *([reason] if reason else []), *details]
        print(" | ".join(fields))
    print("Shadow observations use displayed market data. They do not represent submitted orders or confirmed fills.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=20, help="maximum recent detail rows (default 20)")
    parser.add_argument("--json", action="store_true", help="include recent signal and quote evidence as JSON")
    parser.add_argument("--journal", type=Path, default=oil_d_shadow_path(WTI.directory()))
    args = parser.parse_args()
    if args.hours < 1 or args.limit < 1:
        parser.error("--hours and --limit must be positive")
    now = time.time()
    result = report(args.journal, now - args.hours * 3600, now, limit=args.limit)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_report(result, args.hours)


if __name__ == "__main__":
    main()
