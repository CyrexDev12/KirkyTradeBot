import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend import tracker
from backend.tracker import _desk, collect
from backend.updown.store import Store as UpdownStore
from backend.updown.strategy import Decision, Settings, Window, gold_products

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # 8 AM Arizona


def health(age_s, **extra):
    return {"last_heartbeat": (NOW - timedelta(seconds=age_s)).isoformat(), **extra}


def test_desk_states():
    assert _desk("gold", "Gold", "🥇", NOW, None)["state"] == "down"
    stale = _desk("gold", "Gold", "🥇", NOW, health(600))
    assert stale["state"] == "down" and "no heartbeat for 10 min" in stale["notes"][0]
    assert _desk("gold", "Gold", "🥇", NOW, health(5), warn=True, notes=["errors: x"])["state"] == "warn"
    ok = _desk("gold", "Gold", "🥇", NOW, health(5), starting_balance=100, balance=99)
    assert ok["state"] == "ok" and ok["heartbeat_age_s"] == pytest.approx(5)
    deep = _desk("btc", "BTC", "₿", NOW, health(5), starting_balance=100, balance=75)
    assert deep["state"] == "warn" and "down 25%" in deep["notes"][0]


def _gold_trade(store, start, side="UP", qty=8, price=0.5):
    w = Window("gold", f"cpausp-gold-updown-15m-{start}", start, start + 900, 3500.0, "MARKET_STATUS_OPEN", 0.095,
               "15m", "point")
    cost = qty * (price + 0.095 * price * (1 - price))
    d = Decision("BUY", "test", side=side, fair_up=0.6, fair_side=0.6, price=price, quantity=qty, fee=cost - qty * price,
                 cost=cost, edge=0.05, risk={"max_loss": cost})
    return w, store.open(w, d, start + 120)


def test_collect_reads_gold_journal_and_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    from backend.updown import api as updown_api
    updown_api._store.cache_clear()
    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    store = UpdownStore(gold_dir / "paper.sqlite3")
    start = int(NOW.timestamp()) // 900 * 900 - 1800
    w, won = _gold_trade(store, start)
    store.settle(won["id"], 3510.0, start + 910)
    _gold_trade(store, start + 900, side="DOWN")
    (gold_dir / "health.json").write_text(json.dumps(health(3, errors={})))

    def broken(now, limit=40):
        raise RuntimeError("journal locked")

    collectors = (("gold", "Gold Up/Down", "🥇", tracker._gold), ("nfl", "NFL Polymarket", "🏈", broken))
    status, trades = collect(NOW, collectors)
    gold, nfl = status["desks"]
    assert gold["state"] == "ok" and gold["open_count"] == 1 and gold["record"] == {
        "wins": 1, "losses": 0, "closed": 1, "recent_pnl": pytest.approx(won and trades[-1]["pnl"])}
    assert nfl["state"] == "down" and "could not read" in nfl["notes"][0]
    assert [t["status"] for t in trades] == ["OPEN", "WIN"] and trades[0]["market"] == "gold-15m"
    assert status["totals"]["states"] == {"ok": 1, "warn": 0, "down": 1}
    updown_api._store.cache_clear()


def test_full_roster_totals(tmp_path, monkeypatch):
    from backend.tracker import roster, summarize

    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    from backend.updown import api as updown_api
    updown_api._store.cache_clear()
    (tmp_path / "gold").mkdir()
    store = UpdownStore(tmp_path / "gold" / "paper.sqlite3")
    base = int(NOW.timestamp()) // 900 * 900 - 900 * 60
    for i in range(45):  # 45 settled gold bets: every third one loses
        _, t = _gold_trade(store, base + 900 * i)
        store.settle(t["id"], 3490.0 if i % 3 == 0 else 3510.0, base + 900 * i + 905)
    _gold_trade(store, base + 900 * 45)  # one still open
    (tmp_path / "gold" / "health.json").write_text(json.dumps(health(2)))
    data = roster(NOW, (("gold", "Gold Up/Down", "🥇", tracker._gold),))
    gold = data["desks"][0]
    s = gold["summary"]
    assert len(gold["trades"]) == 46 and s["closed"] == 45 and s["open"] == 1
    assert (s["wins"], s["losses"]) == (30, 15) and s["win_rate"] == pytest.approx(30 / 45, abs=1e-4)
    assert data["totals"]["net_pnl"] == s["net_pnl"] == pytest.approx(sum(t["pnl"] for t in gold["trades"]
                                                                            if t["pnl"] is not None), abs=1e-3)
    assert summarize([])["roi"] is None
    updown_api._store.cache_clear()


def test_tracker_api_is_mounted_and_read_only():
    from pathlib import Path
    from backend.tracker.api import router
    from backend import app as app_module
    assert {(r.path, tuple(sorted(r.methods))) for r in router.routes} == {
        ("/api/tracker/status", ("GET",)), ("/api/tracker/trades", ("GET",)), ("/api/tracker/roster", ("GET",))}
    assert "app.include_router(tracker_router)" in Path(app_module.__file__).read_text()
