from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from backend.app import app
from backend.wti.oil_d_paper_api import chart, status
from backend.wti.oil_d_paper_store import PaperAccount, paper_path


BASE = "/api/wti/paper-execution"
HEADERS = {"X-Paper-Operator-Token": "paper-test-operator"}


@pytest.fixture
def paper_api(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NFL_PAPER_OPERATOR_TOKEN", HEADERS["X-Paper-Operator-Token"])
    monkeypatch.delenv("NFL_PAPER_OPERATOR_TOKEN_FILE", raising=False)
    with closing(TestClient(app)) as client:
        yield client, paper_path(tmp_path / "wti")


def test_operator_routes_wait_for_the_worker_without_writing_a_journal(paper_api):
    client, path = paper_api
    for endpoint in ("/status", "/events", "/chart"):
        assert client.get(BASE + endpoint).status_code == 403
    result = client.get(BASE + "/status", headers=HEADERS).json()
    assert result["state"] == "waiting" and result["mode"] == "PAPER_ONLY"
    assert result["starting_budget"] == "100.00" and not result["available"] and not result["entries_allowed"]
    assert (Decimal(result["entry_cap"]), Decimal(result["loss_cap"])) == (10, 20)
    assert client.get(BASE + "/chart", headers=HEADERS).json()["available"] is False
    assert client.get(BASE + "/events", headers=HEADERS).json() == {"events": [], "latest_id": 0}
    assert client.get(BASE + "/chart?time=bad", headers=HEADERS).status_code == 422
    assert client.get(BASE + "/events?limit=101", headers=HEADERS).status_code == 422
    assert not path.exists()

    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT name FROM sqlite_schema").fetchall() == []
    assert client.get(BASE + "/status", headers=HEADERS).json()["state"] == "waiting"
    assert client.get(BASE + "/chart", headers=HEADERS).json()["available"] is False
    assert client.get(BASE + "/events", headers=HEADERS).json() == {"events": [], "latest_id": 0}


def _legacy_journal(path, now):
    """The previous $10 journal had trades and skips with no events or worker status."""
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE account(id INTEGER PRIMARY KEY,started_at REAL NOT NULL,realized TEXT NOT NULL);
            CREATE TABLE trades(slug TEXT PRIMARY KEY,window_start INTEGER,window_end INTEGER,signal_at REAL,
                sent_at REAL NOT NULL,updated_at REAL,closed_at REAL,state TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE entry_skips(slug TEXT,signal_at REAL,at REAL,reason TEXT);
        """)
        db.execute("INSERT INTO account VALUES(1,?,'-0.56')", (now - 100000,))
        rows = [
            ("older", now - 90000, now - 89950, "closed", {"net_pnl": "-.75", "entry": {"outcome": "filled"}}),
            ("win", now - 3000, now - 2950, "closed", {"net_pnl": ".20", "entry": {"outcome": "filled"}}),
            ("loss", now - 2000, now - 1950, "closed", {"net_pnl": "-.01", "entry": {"outcome": "filled"}}),
            ("open", now - 1000, None, "open", {"side": "UP", "remaining": "1", "entry": {"gross": ".40",
                                                                              "fee": ".01", "outcome": "filled"}}),
        ]
        db.executemany("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)",
                       [(slug, sent, sent + 900, sent, sent, closed or sent, closed, state, json.dumps(payload))
                        for slug, sent, closed, state, payload in rows])


def test_legacy_history_rebases_closed_pnl_on_the_new_budget(paper_api):
    client, path = paper_api
    now = int(time.time())
    _legacy_journal(path, now)
    result = client.get(BASE + "/chart?time=24h", headers=HEADERS).json()
    assert result["available"] and result["open_trade"] and result["starting_budget"] == "100.00"
    assert result["range_pnl"] == "0.19" and result["pilot_pnl"] == "-0.56"
    assert result["closed_in_range"] == 2
    assert [Decimal(point["balance"]) for point in result["points"]] == [Decimal(x) for x in
                                                                       ("99.25", "99.45", "99.44", "99.44")]
    full = chart(path, "all", now)
    assert full["points"][0] == {"at": datetime.fromtimestamp(now - 100000, timezone.utc).isoformat(),
                                 "balance": "100.00"}
    assert full["closed_in_range"] == 3
    health = client.get(BASE + "/status", headers=HEADERS).json()
    assert health["available"] and health["balance"] == "99.44" and health["state"] == "waiting"
    assert client.get(BASE + "/events", headers=HEADERS).json() == {"events": [], "latest_id": 0}
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT realized FROM account").fetchone()[0] == "-0.56"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='events'").fetchone() is None


def test_events_preserve_cursor_and_stale_heartbeat_blocks_entries(paper_api):
    client, path = paper_api
    now = int(time.time())
    PaperAccount(path, now - 60)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT OR REPLACE INTO worker_status VALUES(1,?,1,'scanning',NULL)", (now - 3,))
        db.executemany("INSERT INTO events(at,kind,payload) VALUES(?,?,?)",
                       [(now - 15, "status", '{"entries_allowed":true}'),
                        (now - 8, "entry", '{"side":"UP","balance":"100.00"}'),
                        (now - 5, "closed", '{"balance":"99.95"}')])
    fresh = status(path, now)
    assert fresh["health_current"] and fresh["entries_allowed"] and fresh["state"] == "scanning"
    assert fresh["latest_id"] == 3 and fresh["last_heartbeat"] == datetime.fromtimestamp(now - 3, timezone.utc).isoformat()
    assert status(path, now + 35)["state"] == "offline" and not status(path, now + 35)["entries_allowed"]
    result = client.get(BASE + "/events?after=1&limit=1", headers=HEADERS).json()
    assert result["latest_id"] == 3 and [(event["id"], event["kind"]) for event in result["events"]] == [(2, "entry")]
    assert result["events"][0]["side"] == "UP"
    assert client.get(BASE + "/events?after=3", headers=HEADERS).json() == {"events": [], "latest_id": 3}
