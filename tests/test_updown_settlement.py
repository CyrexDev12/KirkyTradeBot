import asyncio
from datetime import datetime
import json
from pathlib import Path

import pytest

from backend.updown.feed import Feed, FeedError, Settlement
from backend.updown.store import Store
from backend.updown.strategy import Decision, Settings, Window, crypto_products, futures_products
from backend.updown.worker import Worker
from bot.updown import result_label


def test_feed_waits_for_resolution_and_verifies_authoritative_payout():
    path = Path(__file__).parent / "fixtures" / "natgas_capture_2026-11-22.json"
    event = json.loads(path.read_text())["event_15m"]
    market = event["events"][0]["markets"][0]
    terms = market["assetPriceTerms"]
    start = int(datetime.fromisoformat(terms["windowStart"]).timestamp())
    terms["settlementPrice"] = {"value": "3.2679"}
    product = futures_products("natgas", ("15m",), 4)[0]
    slug = market["slug"]

    class Venue:
        calls = []
        response = {"slug": slug, "settlement": 0}

        async def get(self, path, params=None):
            self.calls.append(path)
            return event if path == "/v1/events" else self.response

    venue = Venue()
    feed = Feed(venue, None, clock=lambda: start + 900)
    assert asyncio.run(feed.settlement(product, start)) is None
    assert venue.calls == ["/v1/events"]
    market["status"] = "MARKET_STATUS_RESOLVED"
    # The official payout prevails over a result inferred from display prices.
    assert asyncio.run(feed.settlement(product, start)) == Settlement(slug, 0, 3.2678, 3.2679)
    assert venue.calls[-1] == f"/v1/markets/{slug}/settlement"
    venue.response = {"slug": slug, "settlement": None}
    assert asyncio.run(feed.settlement(product, start)) is None
    venue.response = {"slug": "wrong-market", "settlement": 0}
    with pytest.raises(FeedError, match="does not match"):
        asyncio.run(feed.settlement(product, start))


def test_expired_position_waits_for_official_payout_even_if_index_feed_fails(tmp_path):
    start = 1795374900
    end = start + 900
    now = [end + 7200]
    product = crypto_products(("btc",))[0]
    settings = Settings(products=(product,))

    class PendingFeed:
        result = None

        async def settlement(self, product, start):
            return self.result

        async def seconds(self, ids, lookback=120):
            assert lookback == 120, "settlement must not fetch a substitute close"
            raise FeedError("index temporarily unavailable")

    store = Store(tmp_path / "paper.sqlite3")
    window = Window("btc", "test-market", start, end, 100, "MARKET_STATUS_OPEN", 0.095)
    decision = Decision("BUY", "test trade", side="DOWN", price=0.4, quantity=5, fee=0.10, cost=2.10)
    trade = store.open(window, decision, start + 120)
    store.partial_exit(trade["id"], {"quantity": 2, "price": 0.5, "gross": 1.0,
                                    "fee": 0.03, "fair_side": 0.4}, start + 180)
    feed = PendingFeed()
    worker = Worker(store, feed, settings, clock=lambda: now[0])
    asyncio.run(worker.tick())
    assert store.open_trades()[0]["remaining"] == 3
    assert store.stats()["closed_trades"] == 0
    assert store.stats()["windows_resolved"] == 0
    feed.result = Settlement(window.slug, 0.45, None, None)
    now[0] += 15
    asyncio.run(worker.tick())
    closed = store.trades(1)[0]
    assert closed["status"] == "CLOSED"
    assert closed["outcome"] == "OFFICIAL_PAYOUT"
    assert closed["settlement_source"] == "polymarket"
    assert closed["exits"][-1]["price"] == pytest.approx(0.55)
    assert closed["net_pnl"] == pytest.approx(0.97 + 3 * 0.55 - 2.10)
    assert store.stats()["windows_resolved"] == 0
    assert "official payout of 55¢" in result_label(closed)[1]
    asyncio.run(worker.tick())
    assert [event["kind"] for event in store.events(0, 10)["events"]] == ["opened", "closed"]


def test_old_pending_calibration_windows_do_not_block_new_results(tmp_path):
    product = crypto_products(("btc",))[0]
    start = 1795374900
    starts = [start + index * 900 for index in range(6)]
    now = [starts[-1] + 86400]
    store = Store(tmp_path / "paper.sqlite3")
    for stamp in starts:
        store.note_window(product.key, stamp, "no paper trade", stamp + 120, 100)
        store.observe(product.key, stamp, stamp + 120, fair_up=0.6, yes_bid=0.5, yes_ask=0.51)

    class PendingFeed:
        async def settlement(self, product, start):
            if start == starts[-1]:
                return Settlement("test-market", 1, 100, 101)

    worker = Worker(store, PendingFeed(), Settings(products=(product,)), clock=lambda: now[0])
    asyncio.run(worker._resolve_windows())
    assert store.stats()["windows_resolved"] == 0
    asyncio.run(worker._resolve_windows())
    assert store.stats()["windows_resolved"] == 1
    assert len(store.unresolved_windows({product.key: int(now[0])})) == 5
