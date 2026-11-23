import asyncio
import json
import math
from pathlib import Path
import random

import pytest

from backend.gold import SETTINGS as GOLD_SETTINGS
from backend.updown.feed import event_slug, parse_index, parse_window, point_price, settlement_price, window_start
from backend.updown.model import fair_up_point, fair_value, sigma_per_second
from backend.updown.store import Store
from backend.updown.strategy import Book, Settings, Window, evaluate, gold_products
from backend.updown.worker import Worker
from bot.paper import PaperPosts
from bot.updown import GOLD, trade_row, updown_trade_card

FIX = json.loads((Path(__file__).parent / "fixtures" / "gold_capture_2026-11-20.json").read_text())
START = 1795197600  # 2026-11-20T18:00:00Z
GOLD_SIGMA = 2.5e-5


def test_gold_desk_settings_cover_5m_15m_and_hourly_with_point_settlement():
    s = Settings.from_env({}, prefix="GOLD")
    assert [p.key for p in s.products] == ["gold-5m", "gold-15m", "gold-1h"]
    assert {p.rule for p in s.products} == {"point"} and {p.coinbase for p in s.products} == {"PAXG-USD"}
    assert [p.seconds for p in s.products] == [300, 900, 3600] and s.products[0].index_id == "commodity:gold"
    assert s.coinbase_max_gap == 0.01 and s.max_index_age == 30 and s.max_stake == 0.05
    assert [p.key for p in Settings.from_env({"GOLD_HORIZONS": "1h"}, prefix="GOLD").products] == ["gold-1h"]
    with pytest.raises(ValueError):
        Settings.from_env({"GOLD_HORIZONS": "2m"}, prefix="GOLD")
    assert [p.key for p in GOLD_SETTINGS.products] == ["gold-5m", "gold-15m", "gold-1h"]


def test_point_fair_value():
    assert fair_up_point(3500, 3500, 600, GOLD_SIGMA) == pytest.approx(0.5)
    assert fair_up_point(3500, 3502, 600, GOLD_SIGMA) > fair_up_point(3500, 3502, 3000, GOLD_SIGMA) > 0.5
    assert fair_up_point(3500, 3500, 0, GOLD_SIGMA) == 1.0 and fair_up_point(3500, 3499.99, 0, GOLD_SIGMA) == 0.0
    # No closing-minute average is needed for a point settlement, even in the last seconds.
    assert fair_value("point", 3500, 3501, 20, GOLD_SIGMA) > 0.9
    with pytest.raises(ValueError):
        fair_value("median", 1, 1, 1, 1)


def test_parses_live_gold_windows_and_point_settlement():
    assert event_slug("gold", START, "1h") == "gold-updown-1h-2026-11-20-1800z"
    assert window_start(START + 3599, 3600) == START and window_start(START + 299, 300) == START
    w15 = parse_window("gold", FIX["event_15m"], rule="point")
    w1h = parse_window("gold", FIX["event_1h"], rule="point")
    assert (w15.key, w15.end - w15.start, w15.price_to_beat, w15.rule) == ("gold-15m", 900, 3530.74, "point")
    assert (w1h.key, w1h.end - w1h.start) == ("gold-1h", 3600)
    assert settlement_price(FIX["event_1h"])[1] == 3535.14
    pre = parse_window("gold", FIX["weekend_pre_open"], rule="point")
    assert pre.status == "MARKET_STATUS_CLOSED" and pre.price_to_beat is None
    # The price to beat is the 1-second index price at the window start.
    seconds = parse_index(FIX["index_1s_at_start"])["gold"]
    assert point_price(seconds, START) == w15.price_to_beat
    assert point_price(seconds, START - 100) is None


def gold_window(horizon="15m", ptb=3500.0, start=START):
    seconds = {"5m": 300, "15m": 900, "1h": 3600}[horizon]
    return Window("gold", f"cpausp-gold-updown-{horizon}-x", start, start + seconds, ptb, "MARKET_STATUS_OPEN", 0.095,
                  horizon, "point")


def test_gold_entry_uses_paxg_gap_tolerance():
    w = gold_window()
    book = Book(((0.50, 300),), ((0.52, 300),), START + 300)
    args = dict(now=START + 300, index_price=3502.0, sigma=GOLD_SIGMA, closing_avg=None, balance=100.0,
                open_stake=0.0, today_pnl=0.0, held=False, settings=GOLD_SETTINGS)
    d = evaluate(w, book, coinbase_price=3512.0, **args)  # PAXG ~0.3% premium is fine
    assert d.action == "BUY" and d.side == "UP" and d.cost <= 5
    assert "disagree" in evaluate(w, book, coinbase_price=3550.0, **args).reason  # >1% gap


class GoldFeed:
    """Gold 15m and 1h are listed; 5m is not (as on 2026-11-22)."""

    def __init__(self, clock):
        self.clock, self.settled, self.window_calls = clock, {}, []

    async def window(self, product, start):
        self.window_calls.append(product.key)
        if product.horizon == "5m":
            from backend.updown.feed import FeedError
            raise FeedError(f"no {product.key} market listed for this window")
        return gold_window(product.horizon, start=start)

    async def settlement(self, product, start):
        price = self.settled.get(product.key)
        return ("MARKET_STATUS_RESOLVED", price) if price else ("MARKET_STATUS_OPEN", None)

    async def book(self, slug):
        return Book(((0.50, 300),), ((0.52, 300),), self.clock())

    async def seconds(self, ids, lookback=120):
        now = int(self.clock())
        return {"gold": [(t, 3502.0) for t in range(now - 60, now)]}

    async def minutes(self, ids, lookback=7200):
        rng = random.Random(3)
        closes = [3500.0]
        for _ in range(120):
            closes.append(closes[-1] * math.exp(rng.gauss(0, GOLD_SIGMA * math.sqrt(60))))
        return {"gold": list(enumerate(closes))}

    async def coinbase(self, product_id):
        assert product_id == "PAXG-USD"
        return 3512.0


def test_gold_worker_picks_15m_and_hourly_separately_and_settles_win_and_loss(tmp_path):
    now = [START + 300.0]
    store = Store(tmp_path / "gold.sqlite3")
    feed = GoldFeed(lambda: now[0])
    worker = Worker(store, feed, Settings(products=gold_products(("5m", "15m", "1h")), coinbase_max_gap=0.01,
                                          max_index_age=30), clock=lambda: now[0])
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    assert sorted(t["key"] for t in store.open_trades()) == ["gold-15m", "gold-1h"]
    assert feed.window_calls.count("gold-5m") == 1  # missing series not re-queried within a minute
    assert "no market listed" in " ".join(d["reason"] for d in store.account(now[0])["latest_decisions"])
    now[0] = START + 900 + 20
    feed.settled["gold-15m"] = 3499.0  # Down: the Up pick loses
    asyncio.run(worker.tick())
    now[0] = START + 3600 + 20
    feed.settled["gold-1h"] = 3510.0  # Up: the Up pick wins
    asyncio.run(worker.tick())
    trades = {t["key"]: t for t in store.trades(10)}
    assert trades["gold-15m"]["outcome"] == "DOWN" and trades["gold-15m"]["net_pnl"] < 0
    assert trades["gold-1h"]["outcome"] == "UP" and trades["gold-1h"]["net_pnl"] > 0
    assert store.stats()["by_asset"]["gold-1h"]["wins"] == 1
    # Cards: pick, win, loss, out.
    labels = [updown_trade_card(trades[k], GOLD)["title"] for k in ("gold-1h", "gold-15m")]
    assert labels[0].startswith("✅ WIN · Gold hourly") and labels[1].startswith("❌ LOSS · Gold 15-min")
    assert trade_row({**trades["gold-1h"], "exit_reason": "take_profit"}).startswith("💸 OUT")
    pick = updown_trade_card({**trades["gold-1h"], "status": "OPEN"}, GOLD)
    assert pick["title"].startswith("🥇 PICK · Gold hourly") and "PAXG-USD" in dict(pick["fields"])["Prices used"]
    assert "gold" in pick["footer"]


def test_gold_channel_routing(tmp_path):
    posts = PaperPosts(tmp_path / "posts.sqlite3")
    posts.set_channel(1, "gold", 55)
    posts.set_channel(1, "parlay", 66)
    assert posts.desk_routes("gold", "gold_cursors") == [{"guild_id": 1, "channel_id": 55, "event_id": None}]
    posts.advance_desk("gold_cursors", 1, 7)
    posts.advance_desk("gold_cursors", 1, 3)
    assert posts.desk_routes("gold", "gold_cursors")[0]["event_id"] == 7
    assert posts.configured_nfl_routes() == ["parlay"]
    with pytest.raises(ValueError):
        posts.desk_routes("gold", "paper_posts; DROP TABLE x")


def test_gold_api_is_mounted():
    from backend.gold.api import router
    from backend import app as app_module
    paths = {r.path for r in router.routes}
    assert "app.include_router(gold_router)" in Path(app_module.__file__).read_text()
    assert {"/api/gold/account", "/api/gold/trades", "/api/gold/stats", "/api/gold/events"} <= paths
