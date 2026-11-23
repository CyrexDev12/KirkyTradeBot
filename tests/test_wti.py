"""WTI crude oil desk: markets, daily windows, news calendar, shock/switch detection, the five
models, the shared rules, the multi-model desk, API, Discord cards and Chief tracking."""
import asyncio
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import random

import pytest

from backend.updown.feed import FeedError, Settlement, event_slug, parse_index, parse_window, settlement_price
from backend.updown.strategy import Book, Settings, Window, wti_products
from backend.wti import Rules
from backend.wti.events import ET, Calendar, Detector, Event, cl_last_trade, front_month_expiry
from backend.wti.models import MODEL_INFO, ModelSet, ewma_sigma, point_probability

FIX = json.loads((Path(__file__).parent / "fixtures" / "wti_capture_2026-11-22.json").read_text())
EIA = datetime(2026, 11, 25, 10, 30, tzinfo=ET).timestamp()  # a Wednesday
SIGMA = 3e-5


def ts(*args, tz=timezone.utc):
    return datetime(*args, tzinfo=tz).timestamp()


# ---- markets and windows ----------------------------------------------------------------
def test_wti_settings_markets_and_daily_window():
    s = Settings.from_env({}, prefix="WTI")
    assert [p.key for p in s.products] == ["wti-15m", "wti-1h", "wti-1d"]
    assert {p.coinbase for p in s.products} == {None} and {p.rule for p in s.products} == {"point"}
    assert s.products[0].index_id == "commodity:wti" and s.take_profit_target == 0.5 and s.stop_loss == 0.5
    daily = s.products[2]
    # 10:30 PM ET Sunday -> the daily window that opened at 6:15 PM ET (23:15Z), as listed live.
    start = daily.current_start(ts(2026, 11, 23, 3, 30))
    assert start == ts(2026, 11, 22, 23, 15) and event_slug("wti", start, "1d") == "wti-updown-1d-2026-11-22-2315z"
    assert daily.current_start(ts(2026, 11, 23, 16, 0)) == start  # still the same trading day
    # Summer (EDT): 6:15 PM ET is 22:15Z.
    assert daily.current_start(ts(2026, 7, 14, 23, 0)) == ts(2026, 7, 14, 22, 15)
    assert s.products[0].current_start(ts(2026, 11, 23, 3, 7, 30)) == ts(2026, 11, 23, 3, 0)


def test_parses_live_wti_markets():
    w15 = parse_window("wti", FIX["event_15m"], rule="point")
    d = parse_window("wti", FIX["event_1d"], rule="point")
    assert (w15.key, w15.price_to_beat, w15.end - w15.start) == ("wti-15m", 83.00, 900)
    assert (d.key, d.price_to_beat, d.status) == ("wti-1d", 82.97, "MARKET_STATUS_OPEN")
    assert d.end - d.start == 22 * 3600 + 45 * 60  # 6:15 PM -> 5:00 PM ET
    assert settlement_price(FIX["event_15m"])[1] == 83.19
    assert parse_index(FIX["index_1m_tail"])["wti"] == [(1795403700, 83.25)]


# ---- information: calendar, contract month, detector -------------------------------------
def test_calendar_recurring_file_events_and_skips(tmp_path):
    cal = Calendar(tmp_path / "events.json")
    week = cal.between(ts(2026, 11, 23, 5), ts(2026, 11, 28, 5))
    names = [(datetime.fromtimestamp(e.at, ET).strftime("%a %H:%M"), e.name, e.impact) for e in week
             if "settlement" not in e.name]
    assert names == [("Tue 16:30", "API weekly crude inventory", "medium"),
                     ("Wed 10:30", "EIA Weekly Petroleum Status Report", "high"),
                     ("Fri 13:00", "Baker Hughes rig count", "low")]
    first_friday = [e.name for e in cal.between(ts(2026, 12, 4, 13), ts(2026, 12, 4, 14))]
    assert "US jobs report" in first_friday  # Dec 4 2026 is the first Friday
    (tmp_path / "events.json").write_text(json.dumps({
        "events": [{"at": "2026-11-30T09:00:00", "name": "OPEC+ meeting", "impact": "high", "jump": 0.01}],
        "roll_dates": ["2026-12-18"], "skip": ["2026-11-25 EIA Weekly Petroleum Status Report"]}))
    week = cal.between(ts(2026, 11, 23, 5), ts(2026, 12, 1, 5))
    assert "EIA Weekly Petroleum Status Report" not in [e.name for e in week if e.at < ts(2026, 11, 27)]
    opec = next(e for e in week if e.name == "OPEC+ meeting")
    assert opec.source == "file" and opec.size == 0.01 and datetime.fromtimestamp(opec.at, ET).hour == 9
    assert cal.is_roll_day(ts(2026, 12, 18, 15)) and not cal.is_roll_day(ts(2026, 12, 17, 15))


def test_front_month_contract_dates():
    assert cl_last_trade(2027, 1) == date(2026, 12, 22)  # Dec 25 2026 is a Friday
    assert cl_last_trade(2027, 2) == date(2027, 1, 20)  # Jan 25 2027 is a Monday
    assert cl_last_trade(2027, 3) == date(2027, 2, 22)  # Feb 25 2027 is a Thursday
    assert front_month_expiry(date(2026, 11, 22)) == ("Jan 2027", date(2026, 12, 22))
    assert front_month_expiry(date(2026, 12, 23)) == ("Feb 2027", date(2027, 1, 20))


def test_detector_flags_shocks_and_contract_switch_jumps():
    det, cal = Detector(), Calendar(None)
    base = int(ts(2026, 11, 24, 3, 0))  # a quiet hour, no scheduled news
    calm = [(base + i, 83.0 + 0.001 * (i % 3)) for i in range(60)]
    assert det.update(calm, SIGMA, cal) == []
    shock = calm + [(base + 60 + i, 83.0 * (1.004 if i >= 2 else 1.0)) for i in range(5)]
    found = det.update(shock, SIGMA, cal)
    assert len(found) == 1 and "Price shock +0.40%" in found[0].name and det.last_shock() == base + 62
    jump = shock + [(base + 65 + i, 83.0 * 1.012) for i in range(60)]
    found = det.update(jump, SIGMA, cal)
    assert any("possible contract switch" in e.name for e in found) and det.last_roll() is not None
    # The same jump right after EIA is news, not a contract switch.
    det2 = Detector()
    eia = int(EIA)
    around = [(eia - 60 + i, 83.0) for i in range(60)] + [(eia + i, 83.0 * 1.012) for i in range(60)]
    det2.update(around, SIGMA, cal)
    assert det2.last_roll() is None and det2.last_shock() is not None


# ---- the five models ---------------------------------------------------------------------
def closes(n=180, sigma=SIGMA, seed=1, drift=0.0, start=83.0):
    rng, out = random.Random(seed), [start]
    for _ in range(n):
        out.append(out[-1] * math.exp(drift + rng.gauss(0, sigma * math.sqrt(60))))
    return out


def test_models_agree_on_symmetry_and_differ_where_designed():
    cal, det = Calendar(None), Detector()
    ms = ModelSet(cal, det)
    c = closes()
    flat = [83.0] * 181  # no trend for C to follow
    quiet = ts(2026, 11, 24, 3, 0)
    for m in "ABCDE":
        fn = getattr(ms, f"model_{m.lower()}")
        p = fn(83.0, 83.0, 600, flat if m == "C" else c, None, quiet, quiet + 600)
        assert p == pytest.approx(0.5, abs=0.01 if m == "C" else 1e-9), m
        assert fn(83.0, 83.2, 600, c, None, quiet, quiet + 600) > 0.5
    # E is less sure when EIA falls inside the window, and right after a shock.
    pb = ms.model_b(83.0, 83.08, 900, c, None, EIA - 600, EIA + 300)
    pe = ms.model_e(83.0, 83.08, 900, c, None, EIA - 600, EIA + 300)
    assert 0.5 < pe < pb
    det.shocks.append((quiet - 30, 0.004, 9.0))
    assert 0.5 < ms.model_e(83.0, 83.08, 900, c, None, quiet, quiet + 900) < ms.model_b(83.0, 83.08, 900, c, None,
                                                                                    quiet, quiet + 900)
    # C leans with a confirmed up-trend.
    up = closes(drift=4e-4)  # price sitting right at the price to beat, after a steady climb
    assert ms.model_c(up[-1], up[-1], 900, up, None, quiet, quiet + 900) > 0.5 == \
        ms.model_b(up[-1], up[-1], 900, up, None, quiet, quiet + 900)
    # D sits between model A and the book mid.
    book = Book(((0.30, 100),), ((0.32, 100),), quiet)
    pa = ms.model_a(83.0, 83.1, 600, c, book, quiet, quiet + 600)
    assert 0.31 < ms.model_d(83.0, 83.1, 600, c, book, quiet, quiet + 600) < pa
    assert point_probability(83.0, 83.0, 0, SIGMA) == 1.0
    with pytest.raises(ValueError):
        ewma_sigma(c[:5])
    assert set(MODEL_INFO) == set("ABCDE")


# ---- shared rules: when to stay out and when to get out ----------------------------------
def test_news_gates_and_pre_news_exit():
    cal, det = Calendar(None), Detector()
    rules = Rules(cal, det, hold_before=120, hold_after=60, pre_news_exit=180, shock_cooldown=60, roll_cooldown=1800)
    w = Window("wti", "cpwtif-x", int(EIA) - 600, int(EIA) + 300, 83.0, "MARKET_STATUS_OPEN", 0.095, "15m", "point")
    assert "EIA Weekly Petroleum Status Report" in rules.gate(None, w, EIA - 100)
    assert rules.gate(None, w, EIA - 300) is None and rules.gate(None, w, EIA + 90) is None
    det.shocks.append((EIA - 400, 0.003, 7.0))
    assert "price shock" in rules.gate(None, w, EIA - 380)
    det.rolls.append((EIA - 300, 0.012))
    assert "contract switch" in rules.gate(None, w, EIA - 250)
    trade = {"side": "UP", "remaining": 10, "quantity": 10, "theta": 0.095, "entry_price": 0.50,
             "entry_fee": 10 * 0.095 * 0.25, "window_end_ts": int(EIA) + 300}
    good = Book(((0.58, 100),), ((0.60, 100),), EIA - 150)
    sale = rules.pre_exit(trade, good, EIA - 150, 0.55)
    assert sale["kind"] == "pre_news" and sale["quantity"] == 10 and sale["news"].startswith("EIA")
    assert rules.pre_exit(trade, Book(((0.45, 100),), ((0.47, 100),), EIA - 150), EIA - 150, 0.5) is None  # no loss sale
    assert rules.pre_exit(trade, good, EIA - 900, 0.55) is None  # too early


# ---- the desk: five accounts on one shared feed ------------------------------------------
class FakeFeed:
    def __init__(self, clock):
        self.clock, self.calls, self.settled = clock, {}, {}

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    async def window(self, product, start):
        self._count("window")
        if product.horizon == "1d":
            raise FeedError(f"no {product.key} market listed for this window")
        return Window("wti", f"cpwtif-{product.key}-{start}", start, start + product.seconds, 83.0,
                      "MARKET_STATUS_OPEN", 0.095, product.horizon, "point")

    async def settlement(self, product, start):
        self._count("settlement")
        p = self.settled.get(product.key)
        if p is not None:
            return Settlement(f"cpwtif-{product.key}-{start}", float(p >= 83.0), 83.0, p)

    async def book(self, slug):
        self._count("book")
        return Book(((0.48, 300),), ((0.50, 300),), self.clock())

    async def seconds(self, ids, lookback=120):
        self._count("seconds")
        now = int(self.clock())
        return {"wti": [(t, 83.12) for t in range(now - 60, now)]}

    async def minutes(self, ids, lookback=7200):
        self._count("minutes")
        return {"wti": list(enumerate(closes()))}


def test_desk_runs_five_model_accounts_on_one_feed(tmp_path):
    from backend.wti.worker import Desk
    start = int(ts(2026, 11, 24, 3, 0))  # quiet: no scheduled news
    now = [start + 300.0]
    feed = FakeFeed(lambda: now[0])
    settings = Settings(products=wti_products(("15m", "1h", "1d")), max_index_age=30, take_profit_target=0.5,
                        stop_loss=0.5)
    desk = Desk(feed, settings, models=("A", "B", "C", "D", "E"), directory=tmp_path, clock=lambda: now[0])
    asyncio.run(desk.tick())
    assert feed.calls["seconds"] == 1 and feed.calls["minutes"] == 1  # shared across 5 models
    assert feed.calls["book"] <= 2  # one per market, not one per model
    now[0] += 2
    asyncio.run(desk.tick())
    opened = {m: w.store.open_trades() for m, w in desk.workers.items()}
    assert all(len(v) == 2 for v in opened.values())  # 15m and 1h for every model
    assert {t["model"] for v in opened.values() for t in v} == set("ABCDE")
    assert len({(tmp_path / "models" / m / "paper.sqlite3").stat().st_ino for m in "ABCDE"}) == 5
    now[0] = start + 920
    feed.settled["wti-15m"] = 83.40
    asyncio.run(desk.tick())
    for worker in desk.workers.values():
        closed = [t for t in worker.store.trades(10) if t.get("status") == "CLOSED"]
        assert closed and closed[0]["key"] == "wti-15m"
    health = desk.health()
    assert health["models"] == list("ABCDE") and health["front_month"]["contract"] == "Jan 2027"
    assert health["next_news"]["name"] == "API weekly crude inventory"


def test_desk_holds_off_before_eia_and_logs_news(tmp_path):
    from backend.wti.worker import Desk
    start = int(EIA) - 600
    now = [EIA - 100]
    feed = FakeFeed(lambda: now[0])
    settings = Settings(products=wti_products(("15m",)), max_index_age=30)
    desk = Desk(feed, settings, models=("A", "E"), directory=tmp_path, clock=lambda: now[0])
    asyncio.run(desk.tick())
    assert all(w.store.open_trades() == [] for w in desk.workers.values())
    reasons = [d["reason"] for d in desk.workers["A"].store.account(now[0])["latest_decisions"]]
    assert any("EIA Weekly Petroleum Status Report" in r for r in reasons)
    log = desk.info.since(0)["events"]
    assert any(e["kind"] == "scheduled" and e["name"].startswith("EIA") for e in log)
    assert start  # window start used by the fake feed


# ---- API, Discord and Chief --------------------------------------------------------------
def test_api_leaderboard_info_and_model_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from backend.wti import api
    api._store.cache_clear()
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    board = client.get("/api/wti/models").json()
    assert [m["model"] for m in board["models"]] and board["lead_model"] == "E"
    assert {m["model"] for m in board["models"]} == set("ABCDE") and all(m["balance"] == 100 for m in board["models"])
    info = client.get("/api/wti/info").json()
    assert info["front_month"]["contract"] and isinstance(info["upcoming"], list)
    assert client.get("/api/wti/info/events").json() == {"events": [], "latest_id": 0}
    assert client.get("/api/wti/c/account").json()["model_name"] == "Trend follower"
    assert client.get("/api/wti/z/account").status_code == 404
    api._store.cache_clear()


def test_discord_cards_and_publisher(tmp_path):
    from bot.oil import OilPublisher, info_alert, info_card, leaderboard_card
    from bot.paper import PaperPosts
    from bot.updown import updown_trade_card
    from bot.oil import OIL
    row = {"model": "E", "name": "News-aware", "idea": "Fast volatility plus news", "lead": True, "balance": 103.2,
           "bets": 9, "wins": 3, "losses": 2, "outs": 2, "targets": 1, "stops": 1, "roi": 0.12, "open_count": 1,
           "brier_model": 0.14, "brier_market": 0.15}
    board = leaderboard_card({"models": [row, {**row, "model": "A", "name": "Random walk", "lead": False,
                                               "balance": 98.0}]})
    assert board["fields"][0][0] == "1. E · News-aware ⭐ lead" and "accuracy 0.140 vs market 0.150" in board["fields"][0][1]
    card = info_card({"upcoming": [{"impact": "high", "at_et": "Wed Nov 25 10:30 AM ET",
                                    "name": "EIA Weekly Petroleum Status Report"}],
                      "detected_last_24h": [], "front_month": {"contract": "Jan 2027", "last_trading_day": "2026-12-22",
                                                              "note": "n"}})
    assert "🔴 Wed Nov 25 10:30 AM ET · EIA" in dict(card["fields"])["Scheduled news ahead"]
    assert info_alert({"kind": "scheduled", "name": "EIA", "impact": "high",
                       "at": "2026-11-25T15:30:00+00:00"})["title"] == "📰 Coming up · EIA"
    trade = {"id": 7, "status": "CLOSED", "exit_reason": "pre_news", "asset": "wti", "horizon": "1d", "side": "UP",
             "model": "E", "net_pnl": 0.4, "quantity": 5, "entry_price": 0.5, "cost": 2.6, "proceeds": 3.0,
             "window_start": "2026-11-22T23:15:00+00:00", "window_end": "2026-11-23T22:00:00+00:00",
             "price_to_beat": 82.97, "exits": [{"kind": "pre_news", "price": 0.6}]}
    assert updown_trade_card(trade, OIL.cards)["title"].startswith("📰 OUT BEFORE NEWS · WTI daily")
    assert updown_trade_card(trade, OIL.cards)["title"].endswith("· model E")

    class API:
        def __init__(self):
            self.trades, self.news = [], []

        async def oil(self, path, **params):
            if path == "/models":
                return {"lead_model": "E"}
            if path == "/E/account":
                return {"latest_id": 0}
            if path == "/E/events":
                return {"events": [e for e in self.trades if e["id"] > params.get("after", 0)],
                        "latest_id": len(self.trades)}
            if path == "/info/events":
                return {"events": [e for e in self.news if e["id"] > params.get("after", 0)], "latest_id": len(self.news)}
            raise AssertionError(path)

    class Pub:
        def __init__(self):
            self.sent = []

        async def deliver(self, route, item):
            self.sent.append(item["payload"]["title"])

    posts = PaperPosts(tmp_path / "posts.sqlite3")
    posts.set_channel(1, "oil", 9)
    api, pub = API(), Pub()
    oil = OilPublisher(api, posts, pub)
    asyncio.run(oil.tick())
    assert pub.sent == []  # new channel starts from now
    api.trades = [{"id": 1, "kind": "opened", "trade": {**trade, "status": "OPEN", "fair_side": 0.6, "edge": 0.05,
                                                         "entry_fee": 0.1, "max_loss_pct": 0.03, "payout_if_right": 5,
                                                         "profit_if_right": 2.4}}]
    api.news = [{"id": 1, "kind": "detected", "name": "Price shock +0.40% in 10 s", "impact": "high",
                 "at": "2026-11-24T03:01:00+00:00"}]
    asyncio.run(oil.tick())
    assert pub.sent == ["🛢️ PICK · WTI daily 📈 Up (paper) · model E", "⚡ Oil desk · Price shock +0.40% in 10 s"]
    asyncio.run(oil.tick())
    assert len(pub.sent) == 2


def test_chief_tracks_each_oil_model(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    from backend import tracker
    from backend.updown import api as updown_api
    updown_api._store.cache_clear()
    ids = [c[0] for c in tracker.COLLECTORS]
    assert ids[-10:] == ["oil-A", "oil-B", "oil-C", "oil-D", "oil-E", "gas-A", "gas-B", "gas-C", "gas-D", "gas-E"]
    (tmp_path / "wti").mkdir()
    (tmp_path / "wti" / "health.json").write_text(json.dumps({
        "last_heartbeat": datetime.now(timezone.utc).isoformat(), "errors": {"B:wti-15m": "boom", "watch": "x"}}))
    status, _ = tracker.collect(collectors=tuple(c for c in tracker.COLLECTORS if c[0] in ("oil-A", "oil-B")))
    a, b = status["desks"]
    assert a["name"] == "Oil · A Random walk" and a["balance"] == 100
    assert "boom" not in " ".join(a["notes"]) and "boom" in " ".join(b["notes"])
    updown_api._store.cache_clear()


def test_trend_is_not_stretched_over_a_daily_window():
    ms = ModelSet(Calendar(None), Detector())
    up = closes(drift=2e-4)
    now = ts(2026, 11, 24, 3, 0)
    p_15m = ms.model_c(up[-1], up[-1], 900, up, None, now, now + 900)
    p_day = ms.model_c(up[-1], up[-1], 18 * 3600, up, None, now, now + 18 * 3600)
    assert 0.5 < p_day < p_15m  # same 15-minute lean, diluted by a day's worth of uncertainty
