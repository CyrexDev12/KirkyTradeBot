import asyncio
from dataclasses import replace

import pytest

from backend.updown.feed import FeedError, parse_book
from backend.updown.store import Store, iso
from backend.updown.strategy import Book, Settings, Window, gold_products
from backend.updown.worker import Worker
from backend.wti.worker import SharedFeed

START = 1795431600  # A UTC quarter-hour boundary.


class Market:
    def __init__(self):
        self.now = START + 300
        self.product = gold_products(("15m",))[0]
        self.market = Window("gold", "gold-window", START, START + 900, 100,
                             "MARKET_STATUS_OPEN", .095, rule="point")
        self.depth = Book(((.44, 100),), ((.46, 100),), self.now)
        self.requests = []
        self.delay = 0

    async def window(self, product, start):
        return self.market

    async def coinbase(self, product):
        return 101

    async def seconds(self, ids):
        return {"gold": [(int(self.now), 101)]}

    async def minutes(self, ids):
        return {"gold": [(START + i * 60, 100 + i * .01) for i in range(-40, 1)]}

    async def book(self, slug):
        requested = self.now
        self.requests.append(requested)
        self.now += self.delay
        return replace(self.depth, at=self.now, requested_at=requested)

    async def settlement(self, product, start):
        return None

    def worker(self, path):
        settings = Settings(products=(self.product,), take_profit_target=.5, stop_loss=.5)
        return Worker(Store(path), self, settings, clock=lambda: self.now, fair_fn=lambda *args: .98)


def test_entry_waits_for_arrival_book_and_only_matches_actual_depth(tmp_path):
    market = Market()
    worker = market.worker(tmp_path / "paper.sqlite3")
    asyncio.run(worker.tick())
    assert not worker.store.open_trades()
    signal = market.now
    market.now += 1
    asyncio.run(worker.tick())
    assert len(market.requests) == 1 and not worker.store.open_trades()

    market.now += 1
    market.depth = Book(((.41, 100),), ((.43, 4), (.49, 100)), market.now, sequence=12)
    asyncio.run(worker.tick())
    [trade] = worker.store.open_trades()
    assert market.requests == [signal, signal + 2]
    assert trade["quantity"] == 2 and trade["entry_price"] == .43
    assert trade["entry_fills"] == [[.43, 2]]
    assert trade["book_requested_at"] == iso(signal + 2)
    assert trade["book_sequence"] == 12


def test_entry_cancels_if_arrival_price_exceeds_its_limit(tmp_path):
    market = Market()
    worker = market.worker(tmp_path / "paper.sqlite3")
    asyncio.run(worker.tick())
    market.now += 2
    market.depth = Book(((.49, 100),), ((.50, 100),), market.now)
    asyncio.run(worker.tick())
    assert worker.store.trades(10) == []
    assert worker.store.account(market.now)["latest_decisions"][0]["reason"] == "no fill at the signaled limit"


def test_stop_uses_arrival_bids_and_never_refills_an_unchanged_level(tmp_path):
    market = Market()
    path = tmp_path / "paper.sqlite3"
    worker = market.worker(path)
    asyncio.run(worker.tick())
    market.now += 2
    asyncio.run(worker.tick())
    [trade] = worker.store.open_trades()
    assert trade["quantity"] > 4

    market.now += 3
    market.depth = Book(((.22, 100),), ((.24, 100),), market.now)
    asyncio.run(worker.tick())
    assert not worker.store.open_trades()[0]["exits"]
    market.now += 2
    market.depth = Book(((.17, 4),), ((.20, 100),), market.now, sequence=12)
    asyncio.run(worker.tick())

    # Both the repeated REST payload and the process restart leave depth spent.
    worker = market.worker(path)
    for _ in range(6):
        market.now += 5
        asyncio.run(worker.tick())
    [position] = worker.store.open_trades()
    assert position["remaining"] == trade["quantity"] - 2
    [exit_fill] = position["exits"]
    assert exit_fill["kind"] == "stop_loss" and exit_fill["price"] == .17
    assert exit_fill["fills"] == [[.17, 2]]


def test_execution_read_crossing_close_cannot_book_exit(tmp_path):
    market = Market()
    worker = market.worker(tmp_path / "paper.sqlite3")
    asyncio.run(worker.tick())
    market.now += 2
    asyncio.run(worker.tick())
    [trade] = worker.store.open_trades()
    market.now = market.market.end - 5
    market.depth = Book(((.20, 100),), ((.21, 100),), market.now)
    asyncio.run(worker._manage(trade, {}, {}))
    assert not worker.store.open_trades()[0]["exits"]
    market.now = market.market.end - 1
    market.delay = 2
    asyncio.run(worker._manage(trade, {}, {}))
    [pending] = worker.store.open_trades()
    assert pending["exits"] == [] and pending["remaining"] == trade["quantity"]


def test_shared_feed_replaces_read_requested_before_arrival():
    async def check():
        market = Market()
        market.delay = 1
        feed = SharedFeed(market, lambda: market.now)
        early = await feed.book("gold-window")
        assert early.requested_at < market.now
        current = await feed.execution_book("gold-window", market.now)
        assert current.requested_at == early.at and current is not early
        assert await feed.execution_book("gold-window", current.requested_at) is current
        assert len(market.requests) == 2
    asyncio.run(check())


def test_rejects_unknown_market_state_and_crossed_quotes():
    with pytest.raises(FeedError):
        parse_book({"marketData": {}}, START)
    crossed = {"state": "MARKET_STATE_OPEN", "bids": [{"px": {"value": .8}, "qty": 1}],
               "offers": [{"px": {"value": .7}, "qty": 1}]}
    with pytest.raises(FeedError, match="crossed"):
        parse_book({"marketData": crossed}, START)
