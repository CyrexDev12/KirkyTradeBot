"""Natural gas desk: same energy engine as WTI with gas markets (4-decimal prices), the gas news
calendar (EIA storage, NOAA outlooks), gas contract dates and a wider contract-switch threshold."""
import asyncio
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from backend.updown.feed import event_slug, parse_book, parse_window
from backend.updown.strategy import Book, Settings, Window
from backend.wti.commodity import NATGAS, WTI
from backend.wti.events import ET, Calendar, Detector, front_month_expiry, ng_last_trade

FIX = json.loads((Path(__file__).parent / "fixtures" / "natgas_capture_2026-11-22.json").read_text())
STORAGE = datetime(2026, 11, 26, 10, 30, tzinfo=ET).timestamp()  # a Thursday


def ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def test_gas_settings_markets_and_live_shapes():
    s = Settings.from_env({}, prefix="NATGAS")
    assert [p.key for p in s.products] == ["natgas-15m", "natgas-1h"]  # daily is opt-in
    assert {(p.coinbase, p.decimals, p.index_id) for p in s.products} == {(None, 4, "commodity:natgas")}
    daily = Settings.from_env({"NATGAS_HORIZONS": "15m,1h,1d"}, prefix="NATGAS").products[2]
    start = daily.current_start(ts(2026, 11, 23, 3, 40))
    assert event_slug("natgas", start, "1d") == "natgas-updown-1d-2026-11-22-2315z"
    w = parse_window("natgas", FIX["event_15m"], rule="point")
    d = parse_window("natgas", FIX["event_1d"], rule="point")
    assert (w.key, w.price_to_beat, d.key, d.price_to_beat) == ("natgas-15m", 3.2678, "natgas-1d", 3.2863)
    assert parse_book(FIX["book_1h"], 0).yes_bids[0] == (0.76, 42.0)
    assert NATGAS.price(3.26781) == "$3.2678" and WTI.price(83.2) == "$83.20"


def test_gas_news_calendar_and_contract_dates():
    cal = Calendar(None, NATGAS.recurring, NATGAS.jumps)
    week = [e for e in cal.between(ts(2026, 11, 23, 5), ts(2026, 11, 28, 5)) if e.impact != "low"]
    storage = [e for e in week if e.name == "EIA Weekly Natural Gas Storage Report"]
    assert len(storage) == 1 and storage[0].at == STORAGE and storage[0].impact == "high"
    assert storage[0].size == 0.006  # gas releases move the price more than crude's
    noaa = [e for e in week if e.name.startswith("NOAA")]
    assert len(noaa) == 5 and all(datetime.fromtimestamp(e.at, ET).hour == 15 for e in noaa)
    assert not any("Petroleum" in e.name for e in week)
    assert ng_last_trade(2027, 1) == date(2026, 12, 29) and ng_last_trade(2026, 12) == date(2026, 11, 26)
    assert front_month_expiry(date(2026, 11, 22), NATGAS.expiry) == ("Dec 2026", date(2026, 11, 26))
    assert front_month_expiry(date(2026, 11, 27), NATGAS.expiry) == ("Jan 2027", date(2026, 12, 29))


def test_gas_switch_threshold_is_wider_than_crude():
    base = int(ts(2026, 11, 24, 4, 0))
    path = [(base + i, 3.30) for i in range(60)] + [(base + 60 + i, 3.30 * 1.01) for i in range(60)]
    gas, oil = Detector(roll_jump=NATGAS.roll_jump), Detector(roll_jump=WTI.roll_jump)
    gas.update(path, None, Calendar(None, NATGAS.recurring))
    oil.update(path, None, Calendar(None, WTI.recurring))
    assert gas.last_roll() is None and oil.last_roll() is not None  # a 1% minute is normal-ish for gas


class FakeFeed:
    def __init__(self, clock):
        self.clock, self.settled = clock, {}

    async def window(self, product, start):
        return Window("natgas", f"cpngf-{product.key}-{start}", start, start + product.seconds, 3.2678,
                      "MARKET_STATUS_OPEN", 0.095, product.horizon, "point")

    async def settlement(self, product, start):
        p = self.settled.get(product.key)
        return ("MARKET_STATUS_RESOLVED", p) if p else ("MARKET_STATUS_OPEN", None)

    async def book(self, slug):
        return Book(((0.44, 300),), ((0.46, 300),), self.clock())

    async def seconds(self, ids, lookback=120):
        assert ids == ["commodity:natgas"]
        now = int(self.clock())
        return {"natgas": [(t, 3.2701) for t in range(now - 60, now)]}

    async def minutes(self, ids, lookback=7200):
        return {"natgas": [(i, 3.2678 + 0.0005 * ((i % 5) - 2)) for i in range(181)]}


def test_gas_desk_trades_five_models_and_holds_off_before_storage(tmp_path):
    from backend.wti.worker import Desk
    from backend.updown.strategy import futures_products
    settings = Settings(products=futures_products("natgas", ("15m", "1h"), 4), max_index_age=30)
    start = int(ts(2026, 11, 24, 4, 0))
    now = [start + 300.0]
    desk = Desk(FakeFeed(lambda: now[0]), settings, directory=tmp_path, clock=lambda: now[0], commodity=NATGAS)
    asyncio.run(desk.tick())
    opened = [t for w in desk.workers.values() for t in w.store.open_trades()]
    assert opened and {t["commodity"] for t in opened} == {"natgas"} and {t["model"] for t in opened} <= set("ABCDE")
    health = desk.health()
    assert health["commodity"] == "natgas" and health["front_month"]["contract"] == "Dec 2026"
    assert health["next_news"]["name"].startswith(("NOAA", "EIA Weekly Natural Gas"))
    # Two minutes before the storage report: no new entries for any model.
    later = [STORAGE - 100]
    desk2 = Desk(FakeFeed(lambda: later[0]), settings, directory=tmp_path / "b", clock=lambda: later[0],
                 commodity=NATGAS)
    asyncio.run(desk2.tick())
    assert all(w.store.open_trades() == [] for w in desk2.workers.values())
    assert any("Storage Report" in d["reason"] for d in desk2.workers["A"].store.account(later[0])["latest_decisions"])


def test_gas_index_fallback_keeps_four_decimals(tmp_path):
    from backend.updown.store import Store
    from backend.updown.worker import Worker
    from backend.updown.strategy import futures_products
    product = futures_products("natgas", ("15m",), 4)[0]

    class Feed:
        async def seconds(self, ids, lookback=120):
            return {"natgas": [(1000, 3.26784), (1001, 3.26791)]}

    worker = Worker(Store(tmp_path / "x.sqlite3"), Feed(), Settings(products=(product,)), clock=lambda: 1100)
    assert asyncio.run(worker._index_close(product, 1001)) == 3.2679


def test_gas_api_discord_and_chief(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.natgas.api import router
    from backend.wti import api as energy_api
    energy_api._store.cache_clear()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    board = client.get("/api/natgas/models").json()
    assert board["commodity"] == "natgas" and board["markets"] == ["natgas-15m", "natgas-1h"]
    assert client.get("/api/natgas/info").json()["front_month"]["contract"]
    from bot.oil import GAS, leaderboard_card
    from bot.updown import updown_trade_card
    assert leaderboard_card(board, GAS)["title"] == "🔥 Gas model leaderboard (paper, $100 each)"
    pick = {"id": 1, "status": "OPEN", "asset": "natgas", "horizon": "15m", "side": "DOWN", "model": "E",
            "quantity": 4, "entry_price": 0.46, "entry_fee": 0.1, "cost": 1.94, "fair_side": 0.55, "edge": 0.05,
            "max_loss_pct": 0.02, "payout_if_right": 4, "profit_if_right": 2.06, "price_to_beat": 3.2678,
            "index_price": 3.2701, "window_start": "2026-11-23T03:30:00+00:00",
            "window_end": "2026-11-23T03:45:00+00:00"}
    card = updown_trade_card(pick, GAS.cards)
    assert card["title"] == "🔥 PICK · Nat gas 15-min 📉 Down (paper) · model E"
    assert "price to beat $3.2678" in card["description"] and "$3.2701" in dict(card["fields"])["Prices used"]
    import discord
    from unittest.mock import MagicMock
    from discord import app_commands
    from bot.oil import register_gas_commands
    bot = MagicMock()
    bot.tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    group = register_gas_commands(bot)
    assert group.name == "gas" and {c.name for c in group.commands} == {"leaderboard", "picks", "results", "account",
                                                                        "stats", "info"}
    from backend import tracker
    from backend.updown import api as updown_api
    updown_api._store.cache_clear()
    status, _ = tracker.collect(collectors=tuple(c for c in tracker.COLLECTORS if c[0] == "gas-B"))
    assert status["desks"][0]["name"] == "Gas · B Fast volatility" and status["desks"][0]["emoji"] == "🔥"
    energy_api._store.cache_clear()
    updown_api._store.cache_clear()
