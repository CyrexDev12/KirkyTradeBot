"""Chief Beef Kirkey inside the Beef Kirkey bot: /chiefkirkey commands and the chief channel."""
import asyncio
from datetime import datetime, timedelta, timezone

from bot.paper import PaperPosts
from bot.tracker import (ChiefPublisher, ROSTER_HEADER, alerts_card, bot_card, report_hours, roster_cards,
                         roster_row, status_card, summary_text, trade_key, trade_line)

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # 8 AM Arizona


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
                                                "🎟️ **OPEN** ₿ BTC-USD spot UP · stake $40.00"]

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
    assert {c.value for c in group.get_command("bot").parameters[0].choices} == {"nfl", "btc", "updown", "gold", "oil-A", "oil-B", "oil-C", "oil-D", "oil-E"}


def test_full_roster_cards_and_bracket_results():
    rows = [{"desk": "gold", "id": str(i), "market": "gold-15m", "side": "UP", "stake": 4.0,
             "status": ("WIN", "LOSS", "TARGET", "STOP")[i % 4], "pnl": (3.8, -4.0, 1.9, -2.9)[i % 4],
             "at": "2026-11-23T01:00:00+00:00"} for i in range(45)]
    summary = {"open": 0, "wins": 12, "losses": 11, "outs": 0, "targets": 11, "stops": 11, "flat": 0,
               "closed": 45, "net_pnl": 1.0, "staked": 180.0, "roi": 0.0056, "win_rate": 0.51}
    data = {"as_of": NOW.isoformat(), "totals": summary, "desks": [
        {"id": "gold", "name": "Gold Up/Down", "emoji": "🥇", "state": "ok", "balance": 101, "summary": summary,
         "trades": rows}]}
    cards = roster_cards(data)
    assert "11 take-profit 11 stopped" in cards[0]["description"] and len(cards) == 3
    page = cards[1]["description"].splitlines()
    assert page[1] == ROSTER_HEADER and len({len(x) for x in page[2:-1]}) == 1
    assert len(roster_cards(data, "closed")) == 3 and len(roster_cards(data, "open")) == 1
    assert roster_row(rows[3]).startswith("STOP    gold-15m")
    assert trade_line(rows[2], "🥇") == "🎯 **TARGET** 🥇 gold-15m UP · +$1.90"
    assert trade_line(rows[3], "🥇") == "🛑 **STOP** 🥇 gold-15m UP · -$2.90"
    assert "11 take-profit" in summary_text(summary)
