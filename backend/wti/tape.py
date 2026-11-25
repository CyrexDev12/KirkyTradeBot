"""An execution tape for forward WTI replay. This database has no exchange credentials."""

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3


def tape_path(directory: Path) -> Path:
    return directory / "replay" / "tape.sqlite3"


class Tape:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS markets (
                    slug TEXT PRIMARY KEY, start INTEGER NOT NULL, end INTEGER NOT NULL,
                    window TEXT NOT NULL, payout REAL, settled_at REAL
                );
                CREATE INDEX IF NOT EXISTS tape_market_start ON markets(start);
                CREATE TABLE IF NOT EXISTS tape (
                    id INTEGER PRIMARY KEY, slug TEXT NOT NULL, at REAL NOT NULL,
                    source TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tape_market_time ON tape(slug, source, at, id);
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def record_window(self, window):
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO markets (slug,start,end,window) VALUES (?,?,?,?)
                       ON CONFLICT(slug) DO UPDATE SET window=excluded.window
                       WHERE markets.start=excluded.start AND markets.end=excluded.end
                         AND markets.window<>excluded.window AND markets.settled_at IS NULL""",
                       (window.slug, window.start, window.end, json.dumps(asdict(window), allow_nan=False)))

    def record_settlement(self, settlement, now):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE markets SET payout=?,settled_at=? WHERE slug=? AND settled_at IS NULL",
                       (settlement.yes_payout, now, settlement.slug))

    def append(self, slug, at, source, kind, payload):
        self.append_many([(slug, at, source, kind, payload)])

    def append_many(self, rows):
        with closing(self.connect()) as db, db:
            db.executemany("INSERT INTO tape(slug,at,source,kind,payload) VALUES (?,?,?,?,?)",
                           ((slug, at, source, kind, json.dumps(data, allow_nan=False, separators=(",", ":")))
                            for slug, at, source, kind, data in rows))
