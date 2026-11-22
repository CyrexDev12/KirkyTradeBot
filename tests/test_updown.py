import asyncio
import json
import math
from pathlib import Path
import random

import pytest

from backend.trading.venue import PaperOnlyViolation, check_request
from backend.updown import feed as feedmod
from backend.updown.feed import (closing_minute_average, event_slug, parse_book, parse_index, parse_window,
                                 settlement_price, window_start)
from backend.updown.model import fair_up, sigma_per_second, taker_fee
from backend.updown.store import Store
from backend.updown.strategy import Book, Settings, Window, early_exit, evaluate
from backend.updown.worker import Worker
from bot.updown import updown_account_card, updown_stats_card, updown_trade_card

FIX = json.loads((Path(__file__).parent / "fixtures" / "updown_capture_2026-11-22.json").read_text())
START = 1795374900  # 2026-11-22T19:15:00Z
END = START + 900
SIGMA = 4e-5  # ~0.03% per minute
S = Settings()


def window(ptb=100.0, status="MARKET_STATUS_OPEN"):
    return Window("btc", "cpc-btc-updown-15m-x", START, END, ptb, status, 0.095)


def book(bids=((0.45, 400),), offers=((0.47, 400),)):
    return Book(tuple(bids), tuple(offers), START + 300)


# ---- model -----------------------------------------------------------------
def test_fair_value_symmetry_monotonicity_and_final_minute():
    assert fair_up(100, 100, 600, SIGMA) == pytest.approx(0.5)
    ups = [fair_up(100, p, 600, SIGMA) for p in (99.9, 99.95, 100, 100.05, 100.1)]
    assert ups == sorted(ups) and ups[0] < 0.2 and ups[-1] > 0.8
    # More time left -> less certain.
    assert fair_up(100, 100.05, 60 * 12, SIGMA) < fair_up(100, 100.05, 60 * 3, SIGMA)
    # Inside the final minute the known part of the closing average matters.
    with pytest.raises(ValueError):
        fair_up(100, 100.01, 30, SIGMA)
    assert fair_up(100, 100.0, 30, SIGMA, closing_minute_avg=100.2) > 0.99
    assert fair_up(100, 100.0, 30, SIGMA, closing_minute_avg=99.8) < 0.01
    assert fair_up(100, 1, 1, SIGMA, closing_minute_avg=1e6) == 1.0  # already locked in
    assert fair_up(100, 101, 0, SIGMA, closing_minute_avg=99) == 0.0


def test_sigma_from_minute_closes_and_fee_formula():
    rng = random.Random(1)
    closes = [100.0]
    for _ in range(240):
        closes.append(closes[-1] * math.exp(rng.gauss(0, 0.0006)))
    assert sigma_per_second(closes) == pytest.approx(0.0006 / math.sqrt(60), rel=0.15)
    with pytest.raises(ValueError):
        sigma_per_second(closes[:10])
    assert taker_fee(0.5, 0.095) == pytest.approx(0.02375)
    assert taker_fee(0.9, 0.095) < taker_fee(0.5, 0.095)


# ---- feed parsing (live shapes) -------------------------------------------
def test_parses_live_window_book_index_and_settlement():
    assert event_slug("btc", START) == "btc-updown-15m-2026-11-22-1915z"
    assert window_start(START + 899.9) == START
    w = parse_window("btc", FIX["event"])
    assert (w.slug, w.start, w.end, w.price_to_beat, w.theta) == ("cpc-btc-updown-15m-2026-11-22-1915z", START, END,
                                                                   60640.58, 0.095)
    b = parse_book(FIX["book"], START + 191)
    assert b.yes_bids[0] == (0.46, 993.57) and b.yes_offers[0] == (0.47, 157.0)
    # Down is bought at 1 - best YES bid and sold at 1 - best YES ask.
    assert b.side_asks("DOWN")[0] == (0.54, 993.57) and b.side_bids("DOWN")[0] == (0.53, 157.0)
    idx = parse_index(FIX["index_1s"])
    assert len(idx["btc"]) == 2 and idx["eth"][-1] == (1795375117, 1713.72)  # gap-filled bucket dropped
    assert settlement_price(FIX["resolved_event"]) == ("MARKET_STATUS_RESOLVED", 60677.03)
    assert settlement_price(FIX["event"]) == ("MARKET_STATUS_OPEN", None)
    assert closing_minute_average([(END - 61, 1), (END - 60, 2), (END - 1, 4), (END, 9)], END, END + 5) == 3


def test_venue_allowlist_covers_updown_reads_and_still_blocks_orders():
    check_request("GET", "/v1/events")
    check_request("GET", "/v1/asset-prices/history")
    for method, path in (("POST", "/v1/events"), ("GET", "/v1/orders"), ("DELETE", "/v1/asset-prices/history")):
        with pytest.raises(PaperOnlyViolation):
            check_request(method, path)


# ---- strategy --------------------------------------------------------------
def decide(**kw):
    args = dict(window=window(), book=book(), now=START + 300, index_price=100.03, sigma=SIGMA, closing_avg=None,
                coinbase_price=100.03, balance=100.0, open_stake=0.0, today_pnl=0.0, held=False, settings=S)
    args.update(kw)
    return evaluate(**args)


def test_buys_underpriced_up_with_stated_risk_and_caps():
    d = decide()
    assert d.action == "BUY" and d.side == "UP" and d.fair_side > 0.6
    unit = 0.47 + taker_fee(0.47, 0.095)
    assert d.cost == pytest.approx(d.quantity * unit) and d.cost <= 5.0  # 5% of balance cap
    assert d.risk["max_loss"] == pytest.approx(d.cost, abs=1e-4)
    assert d.risk["payout_if_right"] == d.quantity and d.risk["profit_if_right"] == pytest.approx(d.quantity - d.cost, abs=1e-4)
    # Total open stake cap of 15%.
    assert decide(open_stake=14.0).cost <= 1.0 + 1e-9
    # Only half the displayed size is taken.
    assert decide(book=book(offers=((0.47, 6),))).quantity == 3


def test_buys_down_when_index_is_below_the_price_to_beat():
    d = decide(index_price=99.97, coinbase_price=99.97, book=book(bids=((0.60, 400),), offers=((0.62, 400),)))
    assert d.action == "BUY" and d.side == "DOWN" and d.price == pytest.approx(0.40)


@pytest.mark.parametrize("kw, reason", [
    (dict(held=True), "already holding"),
    (dict(window=window(ptb=None)), "price to beat"),
    (dict(window=window(status="MARKET_STATUS_CLOSED")), "not open"),
    (dict(now=START + 30), "outside the entry"),
    (dict(now=END - 60), "outside the entry"),
    (dict(today_pnl=-10.0, balance=90.0), "daily loss limit"),
    (dict(coinbase_price=None), "Coinbase"),
    (dict(coinbase_price=100.5), "disagree"),
    (dict(index_price=100.0, coinbase_price=100.0), "below"),  # fair 50% vs 47c + fee: edge < 2c
    (dict(index_price=100.3, coinbase_price=100.3, book=book(bids=((0.94, 9),), offers=((0.95, 9),))), "outside the tradable"),
])
def test_skip_reasons(kw, reason):
    d = decide(**kw)
    assert d.action == "SKIP" and reason in d.reason


def test_early_exit_only_when_bid_beats_fair_after_fee():
    rich = book(bids=((0.90, 100),), offers=((0.92, 100),))
    assert early_exit("UP", 10, rich, 0.80, 0.095, S)["quantity"] == 10
    assert early_exit("UP", 10, rich, 0.89, 0.095, S) is None
    # Down holder sells at 1 - YES ask.
    assert early_exit("DOWN", 10, book(bids=((0.05, 100),), offers=((0.08, 100),)), 0.15, 0.095, S)["price"] == pytest.approx(0.92)


# ---- store -----------------------------------------------------------------
def test_store_settles_wins_losses_and_partial_exits(tmp_path):
    store = Store(tmp_path / "u.sqlite3")
    d = decide()
    t = store.open(window(), d, START + 300)
    assert t and store.open(window(), d, START + 301) is None  # one bet per window
    assert store.snapshot(START + 300)["open_stake"] == pytest.approx(d.cost)
    won = store.settle(t["id"], 100.0, END + 10)  # tie settles Up
    assert won["outcome"] == "UP" and won["net_pnl"] == pytest.approx(d.quantity - d.cost, abs=1e-5)

    w2 = Window("eth", "m2", START + 900, END + 900, 100.0, "MARKET_STATUS_OPEN", 0.095)
    d2 = decide(window=w2, now=START + 1200)
    t2 = store.open(w2, d2, START + 1200)
    half = d2.quantity // 2
    part = store.partial_exit(t2["id"], {"quantity": half, "price": 0.9, "gross": 0.9 * half,
                                         "fee": taker_fee(0.9, 0.095) * half, "fair_side": 0.85}, START + 1300)
    assert part["remaining"] == d2.quantity - half
    lost = store.settle(t2["id"], 99.0, END + 910)
    expected = 0.9 * half - taker_fee(0.9, 0.095) * half - d2.cost
    assert lost["outcome"] == "DOWN" and lost["net_pnl"] == pytest.approx(expected, abs=1e-5)
    acct = store.account(END + 1000)
    assert acct["record"] == {"wins": 1, "losses": 1, "flat": 0} and acct["latest_id"] == 4
    assert acct["balance"] == pytest.approx(100 + won["net_pnl"] + lost["net_pnl"], abs=1e-5)
    assert store.stats()["closed_trades"] == 2


# ---- worker end to end ----------------------------------------------------
class FakeFeed:
    def __init__(self, clock):
        self.clock = clock
        self.settled = None
        self.calls = []

    async def window(self, asset, start):
        return Window(asset, f"m-{asset}-{start}", start, start + 900, 100.0, "MARKET_STATUS_OPEN", 0.095)

    async def settlement(self, asset, start):
        return ("MARKET_STATUS_RESOLVED", self.settled) if self.settled else ("MARKET_STATUS_OPEN", None)

    async def book(self, slug):
        return Book(((0.45, 400),), ((0.47, 400),), self.clock())

    async def seconds(self, assets, lookback=120):
        now = int(self.clock())
        return {a: [(t, 100.03) for t in range(now - 60, now)] for a in assets}

    async def minutes(self, assets, lookback=7200):
        rng = random.Random(2)
        closes = [100.0]
        for _ in range(120):
            closes.append(closes[-1] * math.exp(rng.gauss(0, SIGMA * math.sqrt(60))))
        return {a: list(enumerate(closes)) for a in assets}

    async def coinbase(self, asset):
        return 100.03


def test_worker_enters_once_per_window_journals_and_settles(tmp_path):
    now = [START + 30.0]
    store = Store(tmp_path / "u.sqlite3")
    fake = FakeFeed(lambda: now[0])
    worker = Worker(store, fake, Settings(assets=("btc", "eth")), clock=lambda: now[0])
    asyncio.run(worker.tick())
    assert store.open_trades() == []  # too early in the window
    now[0] = START + 300
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    opened = store.open_trades()
    assert sorted(t["asset"] for t in opened) == ["btc", "eth"]
    assert all(t["cost"] <= 5 and t["coinbase_price"] == 100.03 for t in opened)
    now[0] = END + 20
    fake.settled = 100.5
    asyncio.run(worker.tick())
    assert store.open_trades() == [] and store.stats()["wins"] == 2
    assert store.stats()["model_brier"] is not None


# ---- Discord cards ---------------------------------------------------------
def test_cards_state_the_risk(tmp_path):
    store = Store(tmp_path / "u.sqlite3")
    t = store.open(window(), decide(), START + 300, {"index_price": 100.03, "coinbase_price": 100.02, "seconds_left": 600})
    card = updown_trade_card(t)
    risk = dict(card["fields"])["⚠️ Risk"]
    assert "most you can lose" in risk and "5% max per bet" in risk and "10% daily loss" in risk
    closed = updown_trade_card(store.settle(t["id"], 101, END + 5))
    assert "closed" in closed["title"] and "settled **UP**" in closed["description"]
    acct = store.account(END + 10)
    acct["risk_settings"] = {"assets": ["btc"], "min_edge": 0.02, "price_range": [0.1, 0.9], "kelly_fraction": 0.25,
                             "max_stake_pct": 0.05, "max_open_stake_pct": 0.15, "daily_loss_limit": 0.1}
    assert "daily stop 10%" in dict(updown_account_card(acct)["fields"])["⚠️ Risk settings"]
    assert updown_stats_card(store.stats())["title"]
