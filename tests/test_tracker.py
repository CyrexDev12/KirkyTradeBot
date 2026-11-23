import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend import tracker
from backend.tracker import _desk, collect
from backend.updown.store import Store as UpdownStore
from backend.updown.strategy import Decision, Settings, Window, gold_products
from bot.paper import PaperPosts
from bot.tracker import (ChiefPublisher, alerts_card, bot_card, report_hours, status_card, trade_key,
                         trade_line)

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


class FakeAPI:
    def __init__(self):
        self.status = {"as_of": NOW.isoformat(), "desks": [
            {"id": "gold", "name": "Gold Up/Down", "emoji": "🥇", "state": "ok", "heartbeat_age_s": 3, "notes": [],
             "starting_balance": 100, "balance": 101.5, "today_pnl": 1.5, "open_count": 1, "at_risk": 4.1,
             "record": {"wins": 1, "losses": 0, "closed": 1}},
            {"id": "btc", "name": "BTC spot (Coinbase)", "emoji": "₿", "state": "ok", "heartbeat_age_s": 2,
             "notes": [], "starting_balance": 100, "balance": 100, "today_pnl": 0, "open_count": 0, "at_risk": 0,
             "record": {"wins": 0, "losses": 0, "closed": 0}}],
            "totals": {"balance": 201.5, "starting_balance": 200, "today_pnl": 1.5, "open_count": 1, "at_risk": 4.1,
                       "states": {"ok": 2, "warn": 0, "down": 0}}}
        self.trades = [{"desk": "gold", "id": "1", "market": "gold-15m", "side": "UP", "stake": 4.1,
                        "status": "OPEN", "pnl": None, "at": "2026-11-23T14:50:00+00:00"}]

    async def tracker(self, path, **params):
        return self.status if path == "/status" else {"trades": self.trades[: params.get("limit", 30)]}


class FakePublisher:
    def __init__(self):
        self.sent = []

    async def deliver(self, route, item):
        self.sent.append((route["channel_id"], item["publication_id"], item["payload"]))
        return len(self.sent)


def test_chief_feed_alerts_and_reports(tmp_path):
    posts = PaperPosts(tmp_path / "posts.sqlite3")
    posts.set_channel(1, "chief", 77)
    api, pub, now = FakeAPI(), FakePublisher(), [NOW - timedelta(hours=2)]
    chief = ChiefPublisher(api, posts, pub, clock=lambda: now[0], hours=(8, 20))
    asyncio.run(chief.tick())
    assert len(pub.sent) == 1 and "reporting for duty" in pub.sent[0][2]["title"]  # no backlog flood
    asyncio.run(chief.tick())
    assert len(pub.sent) == 1  # nothing new

    # A trade closes as a win and a new one opens: one feed card with both.
    api.trades = [{"desk": "btc", "id": "slot-2", "market": "BTC-USD spot", "side": "UP", "stake": 40.0,
                   "status": "OPEN", "pnl": None, "at": "2026-11-23T14:55:00+00:00"},
                  {**api.trades[0], "status": "WIN", "pnl": 3.9}] + api.trades
    asyncio.run(chief.tick())
    feed = pub.sent[-1][2]
    assert "trade tracker · 1W 0L" in feed["title"]
    assert feed["description"].splitlines() == ["✅ **WIN** 🥇 gold-15m UP · +$3.90",
                                                "🎯 **OPEN** ₿ BTC-USD spot UP · stake $40.00"]

    # The gold bot stops: one alert, then a recovery alert.
    api.status["desks"][0] = {**api.status["desks"][0], "state": "down", "notes": ["no heartbeat for 6 min"]}
    asyncio.run(chief.tick())
    asyncio.run(chief.tick())
    alerts = [p for _, pid, p in pub.sent if pid.startswith("chief:alert")]
    assert len(alerts) == 1 and alerts[0]["title"] == "🔴 🥇 Gold Up/Down is down"
    api.status["desks"][0] = {**api.status["desks"][0], "state": "ok", "notes": []}
    asyncio.run(chief.tick())
    assert pub.sent[-1][2]["title"] == "🟢 🥇 Gold Up/Down is back to normal"

    # 8 AM Arizona: one status report per slot.
    now[0] = NOW
    asyncio.run(chief.tick())
    asyncio.run(chief.tick())
    reports = [p for _, pid, p in pub.sent if pid.startswith("chief:report")]
    assert len(reports) == 1 and reports[0]["title"] == "🎖️ Chief Beef Kirkey · 8 am report"
    assert all(c == 77 for c, _, _ in pub.sent)
    assert posts.configured_nfl_routes() == []


def test_cards():
    api = FakeAPI()
    card = status_card(api.status)
    assert "2 healthy" in card["description"] and "$201.50" in card["description"]
    assert card["fields"][0][0] == "🟢 🥇 Gold Up/Down" and "1 open ($4.10 at risk)" in card["fields"][0][1]
    assert "all bots healthy" in alerts_card(api.status)["title"]
    api.status["desks"][1]["state"] = "warn"
    assert "1 bot(s) need attention" in alerts_card(api.status)["title"]
    deep = bot_card(api.status, api.trades, "gold")
    assert deep["title"] == "🟢 🥇 Gold Up/Down" and "OPEN" in dict(deep["fields"])["Recent trades"]
    assert trade_line({"desk": "nfl", "id": "x", "market": "KC-BUF · moneyline", "side": "KC", "stake": 9,
                       "status": "LOSS", "pnl": -9}, "🏈") == "❌ **LOSS** 🏈 KC-BUF · moneyline KC · -$9.00"
    assert trade_key(api.trades[0]) == "gold:1:OPEN"
    assert report_hours({"CHIEF_REPORT_HOURS": "20, 8"}) == (8, 20) and report_hours({}) == (8, 20)


def test_chiefkirkey_command_group():
    import discord
    from unittest.mock import MagicMock
    from discord import app_commands
    from bot.tracker import register_chief_commands

    bot = MagicMock()
    bot.tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    register_chief_commands(bot)
    group = bot.tree.get_command("chiefkirkey")
    assert {c.name for c in group.commands} == {"status", "trades", "bot", "fullroster", "alerts"}
    roster_cmd = group.get_command("fullroster")
    assert [p.display_name for p in roster_cmd.parameters] == ["bot", "show"]
    assert {c.value for c in roster_cmd.parameters[1].choices} == {"all", "open", "closed"}
    trades = group.get_command("trades")
    assert [p.display_name for p in trades.parameters] == ["bot"] and not trades.parameters[0].required
    assert {c.value for c in group.get_command("bot").parameters[0].choices} == {"nfl", "btc", "updown", "gold"}


def test_full_roster_totals_and_clean_tables(tmp_path, monkeypatch):
    from backend.tracker import roster, summarize
    from bot.tracker import ROSTER_HEADER, ROSTER_LINES, roster_cards, roster_row

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
    cards = roster_cards(data)
    assert cards[0]["title"] == "🎖️ Chief Beef Kirkey · full roster" and "30W 15L" in cards[0]["description"]
    pages = cards[1:]
    assert [c["title"] for c in pages] == ["🥇 Gold Up/Down · 46 trade(s) · page 1/2",
                                           "🥇 Gold Up/Down · 46 trade(s) · page 2/2"]
    body = pages[0]["description"].splitlines()
    assert body[0] == "```" and body[1] == ROSTER_HEADER and len(body) == ROSTER_LINES + 3
    widths = {len(line.rstrip()) - len(line.rstrip().split("  ")[-1]) for line in body[2:-1]}
    assert len(widths) == 1  # every row's columns line up
    assert body[2].startswith("OPEN    gold-15m") and " open " in body[2]
    assert len(roster_cards(data, "open")) == 2 and "open trades" in roster_cards(data, "open")[0]["title"]
    assert all(len(c["description"]) < 4000 for c in cards)
    row = roster_row({"status": "LOSS", "market": "KC-BUF · moneyline game 12", "side": "KC", "stake": 12.5,
                      "pnl": -12.5, "at": "2026-11-23T01:05:00+00:00"})
    assert row == "LOSS    KC-BUF · moneyline ga… KC       $12.50   -$12.50  11/22 6:05p"
    assert summarize([])["roi"] is None
    updown_api._store.cache_clear()
