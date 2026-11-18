"""Persistent server settings and an outbox for restart-safe kickoff alerts."""
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                    minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    guild_id INTEGER NOT NULL, game_id TEXT NOT NULL, channel_id INTEGER NOT NULL,
                    kickoff TEXT NOT NULL, started_at TEXT NOT NULL, lease_until TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,
                    message_id INTEGER, error TEXT, PRIMARY KEY (guild_id, game_id)
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def configure(self, guild_id, channel_id, minutes):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?,1)", (guild_id, channel_id, minutes))

    def disable(self, guild_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE settings SET enabled=0 WHERE guild_id=?", (guild_id,))

    def settings(self, guild_id=None):
        with closing(self.connect()) as db:
            if guild_id is not None:
                row = db.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
                return dict(row) if row else None
            return [dict(r) for r in db.execute("SELECT * FROM settings WHERE enabled=1")]

    def delivered(self, guild_id, game_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT message_id FROM deliveries WHERE guild_id=? AND game_id=?",
                             (guild_id, game_id)).fetchone()
            return bool(row and row["message_id"])

    def claim(self, settings, game, payload, now):
        key = (settings["guild_id"], game["game_id"])
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE guild_id=? AND game_id=?", key).fetchone()
            if row and (row["message_id"] or row["lease_until"] > now.isoformat()):
                return None
            lease = (now + timedelta(minutes=5)).isoformat()
            if row:
                db.execute("UPDATE deliveries SET attempts=attempts+1,lease_until=? WHERE guild_id=? AND game_id=?",
                           (lease, *key))
            else:
                db.execute("""INSERT INTO deliveries
                    (guild_id,game_id,channel_id,kickoff,started_at,lease_until,payload)
                    VALUES (?,?,?,?,?,?,?)""",
                           (*key, settings["channel_id"], game["kickoff"], now.isoformat(), lease,
                            json.dumps(payload, allow_nan=False)))
            return dict(db.execute("SELECT * FROM deliveries WHERE guild_id=? AND game_id=?", key).fetchone())

    def prepare_retry(self, row, channel_id, game, payload):
        # Call only after checking the original channel for an uncertain prior send.
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE deliveries SET channel_id=?,kickoff=?,payload=?
                          WHERE guild_id=? AND game_id=? AND message_id IS NULL""",
                       (channel_id, game["kickoff"], json.dumps(payload, allow_nan=False), row["guild_id"], row["game_id"]))

    def mark_sent(self, row, message_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE deliveries SET message_id=?,error=NULL WHERE guild_id=? AND game_id=?",
                       (message_id, row["guild_id"], row["game_id"]))

    def failed(self, row, error, now):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE deliveries SET error=?,lease_until=? WHERE guild_id=? AND game_id=?",
                       (error, now.isoformat(), row["guild_id"], row["game_id"]))

    def last_delivery(self, guild_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM deliveries WHERE guild_id=? ORDER BY started_at DESC LIMIT 1",
                             (guild_id,)).fetchone()
            return dict(row) if row else None
