import asyncio
from contextlib import closing
from datetime import datetime, timezone
from email.utils import format_datetime
import sqlite3
import time

import httpx
import pytest

from backend.markets import _get
from backend.trading.request_budget import RequestBudget, retry_after_seconds
from backend.trading.venue import PublicRequestDeadline, ReadOnlyVenueClient, public_request_scope
from backend.updown.feed import Feed


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def test_shared_budget_grants_only_one_simultaneous_slot(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.sqlite3"
    first = RequestBudget(path, clock=clock)
    second = RequestBudget(path, clock=clock)

    async def reserve():
        return await asyncio.gather(first.acquire(deadline=100.1), second.acquire(deadline=100.1))

    assert sorted(asyncio.run(reserve())) == [False, True]


def test_venue_cooldown_is_shared_by_all_priorities(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.sqlite3"
    first = RequestBudget(path, clock=clock)
    second = RequestBudget(path, clock=clock)
    first.set_live_mode(enabled=True, exposure=True)

    assert first.observe_rate_limit("5") == 105
    clock.now = 101
    assert second.observe_rate_limit("2") == 105
    assert second.snapshot() == {
        "live_enabled": True, "live_exposure": True, "cooldown_seconds": 4, "last_public_request_at": None,
        "nfl_paper_extra_cooldown_seconds": 0,
    }
    for priority in ("background", "nfl_paper", "wti_signal", "live", "exposure"):
        assert not asyncio.run(second.acquire(priority=priority, deadline=104))

    clock.now = 105
    assert asyncio.run(second.acquire(priority="exposure", deadline=106))


def test_retry_after_accepts_http_dates_and_ignores_invalid_headers():
    retry_at = format_datetime(datetime.fromtimestamp(106, timezone.utc), usegmt=True)
    assert retry_after_seconds(retry_at, 100) == 6
    assert retry_after_seconds("4", 100) == 4
    assert retry_after_seconds("0", 100) == 1
    assert retry_after_seconds("invalid", 100) is None
    assert retry_after_seconds("nan", 100) is None


def test_fallback_backoff_grows_after_repeated_rejections(tmp_path):
    clock = Clock()
    budget = RequestBudget(tmp_path / "budget.sqlite3", clock=clock)

    assert budget.observe_rate_limit() == 101
    assert budget.observe_rate_limit() == 101  # Same burst.
    clock.now = 101
    assert budget.observe_rate_limit() == 103
    clock.now = 103
    assert budget.observe_rate_limit() == 107
    clock.now = 138
    assert budget.observe_rate_limit() == 139


def test_live_mode_reserves_headroom_until_pilot_is_off_and_flat(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.sqlite3"
    live = RequestBudget(path, clock=clock)
    paper = RequestBudget(path, clock=clock)

    assert not paper.live_priority_active()
    live.set_live_mode(enabled=True)
    assert paper.live_priority_active()
    assert asyncio.run(paper.acquire(deadline=100.1))
    clock.now = 100.125
    assert not asyncio.run(paper.acquire(deadline=100.45))
    assert asyncio.run(live.acquire(priority="live", deadline=100.45))

    live.set_live_mode(enabled=False, exposure=True)
    clock.now = 100.5
    assert paper.live_priority_active()
    assert not asyncio.run(paper.acquire(deadline=100.7))
    assert asyncio.run(live.acquire(priority="exposure", deadline=100.7))

    live.set_live_mode(enabled=False)
    clock.now = 100.625
    assert not paper.live_priority_active()
    assert asyncio.run(paper.acquire(deadline=100.7))


def test_live_mode_lease_expires_when_worker_stops(tmp_path):
    clock = Clock()
    budget = RequestBudget(tmp_path / "budget.sqlite3", clock=clock)

    budget.set_live_mode(enabled=True, exposure=True, lease_seconds=1)
    clock.now = 100.999
    assert budget.live_priority_active()
    clock.now = 101
    assert not budget.live_priority_active()


def test_priority_lanes_pace_nfl_paper_without_delaying_live_signal_data(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.sqlite3"
    live = RequestBudget(path, clock=clock)
    paper = RequestBudget(path, clock=clock)
    live.set_live_mode(enabled=True)
    assert asyncio.run(paper.acquire("nfl_paper", deadline=100.1))
    clock.now = 100.125
    assert not asyncio.run(paper.acquire(deadline=100.45))
    assert asyncio.run(paper.acquire("wti_signal", deadline=100.2))
    clock.now = 100.25
    assert not asyncio.run(paper.acquire("wti_signal", deadline=100.5))
    assert asyncio.run(live.acquire("live", deadline=100.5))
    clock.now = 100.5
    assert asyncio.run(paper.acquire(deadline=100.6))
    assert not asyncio.run(paper.acquire("nfl_paper", deadline=104.9))
    clock.now = 105
    assert asyncio.run(paper.acquire("nfl_paper", deadline=105.1))

    live.set_live_mode(enabled=False, exposure=True)
    clock.now = 110
    assert not asyncio.run(paper.acquire("nfl_paper", deadline=112.9))
    assert asyncio.run(paper.acquire("wti_signal", deadline=110.1))
    clock.now = 110.125
    assert asyncio.run(live.acquire("exposure", deadline=110.2))

    live.set_live_mode(enabled=False)
    clock.now = 110.25
    assert asyncio.run(paper.acquire("nfl_paper", deadline=110.3))
    clock.now = 110.375
    assert asyncio.run(paper.acquire("nfl_paper", deadline=110.4))
    clock.now = 110.5
    assert asyncio.run(paper.acquire("wti_signal", deadline=110.6))
    clock.now = 110.625
    assert asyncio.run(paper.acquire("wti_signal", deadline=110.7))


def test_armed_nfl_429_resumes_live_first_without_bypassing_global_cooldown(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.sqlite3"
    live = RequestBudget(path, clock=clock)
    paper = RequestBudget(path, clock=clock)
    live.set_live_mode(enabled=True)
    assert paper.observe_rate_limit("2", "nfl_paper") == 102
    assert live.snapshot()["nfl_paper_extra_cooldown_seconds"] == 20
    assert not asyncio.run(live.acquire("live", deadline=101.9))
    assert not asyncio.run(paper.acquire("wti_signal", deadline=101.9))

    clock.now = 102
    assert asyncio.run(live.acquire("live", deadline=102.1))
    assert not asyncio.run(paper.acquire("nfl_paper", deadline=110))
    clock.now = 102.125
    assert asyncio.run(paper.acquire("wti_signal", deadline=102.2))

    live.set_live_mode(enabled=False)
    clock.now = 102.25
    assert live.snapshot()["nfl_paper_extra_cooldown_seconds"] == 0
    assert asyncio.run(paper.acquire("nfl_paper", deadline=102.3))


def test_reopen_previous_budget_keeps_existing_cooldown(tmp_path):
    clock = Clock()
    path = tmp_path / "budget.sqlite3"
    previous = RequestBudget(path, clock=clock)
    previous.set_live_mode(enabled=True)
    previous.observe_rate_limit("5")
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE priority_lane")

    current = RequestBudget(path, clock=clock)
    assert current.live_priority_active()
    assert not asyncio.run(current.acquire("wti_signal", deadline=104.9))


def test_budget_requires_an_explicit_persistent_path(tmp_path, monkeypatch):
    monkeypatch.delenv("NFL_VENUE_BUDGET_DB", raising=False)
    assert RequestBudget.from_env() is None

    path = tmp_path / "shared" / "venue-budget.sqlite3"
    monkeypatch.setenv("NFL_VENUE_BUDGET_DB", str(path))
    budget = RequestBudget.from_env()
    assert budget.path == path
    assert RequestBudget.from_env() is budget
    assert path.is_file()

    monkeypatch.setenv("NFL_VENUE_BUDGET_DB", str(path.with_name("other.sqlite3")))
    assert RequestBudget.from_env() is not budget


def test_worker_429_pauses_dashboard_and_another_live_public_read(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_VENUE_BUDGET_DB", str(tmp_path / "budget.sqlite3"))
    requests = []

    def send(request):
        requests.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "20"})

    async def run():
        async with httpx.AsyncClient(base_url="https://gateway.polymarket.us",
                                     transport=httpx.MockTransport(send)) as http:
            paper = ReadOnlyVenueClient(http, max_retries=0, priority="nfl_paper")
            live = ReadOnlyVenueClient(http, max_retries=0)
            paper.budget.set_live_mode(enabled=True)
            with pytest.raises(httpx.HTTPStatusError, match="429"):
                await paper.get("/v1/markets/nfl-test/book")
            assert live.budget.snapshot()["nfl_paper_extra_cooldown_seconds"] == 20
            with public_request_scope("live", time.time() + 6):
                with pytest.raises(PublicRequestDeadline):
                    await live.get("/v1/markets/wti-test/book")
            with pytest.raises(PublicRequestDeadline):
                await _get(http, "/v2/leagues/nfl/events")

    asyncio.run(run())
    assert requests == ["/v1/markets/nfl-test/book"]


def test_book_timestamp_starts_at_http_dispatch_after_pacing():
    clock = Clock()

    class Pacer:
        async def acquire(self, priority, deadline):
            clock.now = 100.4
            return True

    def response(request):
        clock.now = 101.0
        return httpx.Response(200, json={"marketData": {"state": "MARKET_STATE_OPEN", "marketSlug": "wti",
            "transactTime": "1970-01-01T00:01:40.400Z", "bids": [], "offers": []}})

    async def run():
        async with httpx.AsyncClient(base_url="https://gateway.polymarket.us",
                                     transport=httpx.MockTransport(response)) as http:
            venue = ReadOnlyVenueClient(http, max_retries=0)
            pacer = Pacer()
            pacer.clock = clock
            venue.budget = pacer
            return await Feed(venue, http, clock=clock).book("wti")

    book = asyncio.run(run())
    assert book.requested_at == 100.4 and book.exchange_at == 100.4 and book.at == 101.0
