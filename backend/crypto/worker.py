"""Independent paper worker for alternate BTC 15-minute windows (Arizona time)."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import signal as os_signal

import httpx

from .market import DataUnavailable, Market
from .store import Store, data_directory
from .strategy import ENTRY_GRACE, current_slot, trend

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


class Worker:
    def __init__(self, store, market, clock=utcnow):
        self.store, self.market, self.clock = store, market, clock
        self.pending_slot = None
        self.data_error = None
        self.retry_after = None

    async def tick(self):
        now = self.clock()
        slot = current_slot(now)
        # Never manufacture an entry after its grace period, even after downtime.
        if self.pending_slot and now >= self.pending_slot + ENTRY_GRACE:
            self.store.decide(self.pending_slot, now, self.data_error or "entry window expired")
            self.pending_slot = None
        if slot and not self.store.has_slot(slot):
            if now >= slot + ENTRY_GRACE:
                self.store.decide(slot, now, "entry window missed")
            else:
                self.pending_slot = slot
        position = self.store.open_trade()
        if not position and not self.pending_slot:
            return
        if self.retry_after and now < self.retry_after:
            return
        self.data_error = None
        try:
            if position:
                quote = await self.market.quote()
                now = self.clock()
                quote.validate(now)
                self.store.observe(position["slot_at"], quote, now)
            if self.pending_slot:
                await self.evaluate_slot(self.pending_slot)
                self.pending_slot = None
        except (httpx.HTTPError, DataUnavailable, ValueError) as exc:
            self.data_error = f"{type(exc).__name__}: {exc}"[:300]
            self.retry_after = self.clock() + timedelta(seconds=5)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                try:
                    delay = max(10, int(exc.response.headers.get("retry-after", 15)))
                except ValueError:
                    delay = 15
                self.retry_after = self.clock() + timedelta(seconds=min(delay, 86400))
            logger.warning("BTC data unavailable: %s", self.data_error)

    async def evaluate_slot(self, slot):
        if self.store.has_slot(slot):
            return
        bars = await self.market.bars(slot)
        signal = trend(bars)
        quote = None
        if signal.direction:
            quote = await self.market.quote()
            quote.validate(self.clock())
            if quote.at < slot:
                raise DataUnavailable("ticker predates the entry window")
        now = self.clock()
        if not slot <= now < slot + ENTRY_GRACE:
            self.store.decide(slot, now, "entry window expired while retrieving data")
        else:
            self.store.decide(slot, now, None, signal, quote)


async def serve():
    directory = data_directory()
    store = Store(directory / "paper.sqlite3")
    path = directory / "health.json"
    stop = asyncio.Event()
    for signum in (os_signal.SIGTERM, os_signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "nfl-fair-value-paper/1"}) as client:
        worker = Worker(store, Market(client))
        while not stop.is_set():
            try:
                await worker.tick()
                error = worker.data_error
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:300]
                logger.exception("BTC worker cycle failed")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_heartbeat": utcnow().isoformat(), "last_error": error}))
            tmp.replace(path)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
