import asyncio
from decimal import Decimal

import pytest

from backend.wti.live_store import PILOT_LOSS_PAUSE, LiveStore, live_path
from backend.wti.live_worker import Pilot, begin_pilot


D = Decimal
OPERATOR_PAUSE = "New entries stopped by operator. Existing positions stay under management."
T0 = 1_795_573_200
CONFIGURED = {"worker_configured": True, "entries_configured": True}


class Account:
    def __init__(self, funds="10.61", positions=None, orders=()):
        self.funds = D(funds)
        self.held = {} if positions is None else positions
        self.orders = orders
        self.reads = []

    async def positions(self):
        self.reads.append("positions")
        return self.held

    async def open_orders(self):
        self.reads.append("orders")
        return self.orders

    async def buying_power(self):
        self.reads.append("buying_power")
        return self.funds


def add_closed_trade(store, at, net_pnl):
    slug = f"wti-{at}"
    signal = store.record_signal(slug, at - 1, at + 899, at, {"deadline": at + 850})
    order = store.claim_entry(signal, f"key-{at}", at, {"side": "UP", "price": ".5", "quantity": "1"})
    assert store.authorize_entry(order["id"], at + 1)
    store.update_order(order["id"], "ORDER_STATE_FILLED", at + 2, venue_id=f"venue-{at}")
    store.close_trade(order["trade_id"], at + 3, "CLOSED", {"net_pnl": D(net_pnl), "signal_at": at})
    return order["trade_id"]


def begin(store, venue, *, key="oil-d-v2", budget="10", configured="12", at=T0):
    return asyncio.run(begin_pilot(store, venue, key, D(budget), configured_cap=D(configured), clock=lambda: at))


def test_begin_paused_funded_pilot_without_rewriting_history_or_require_exact_balance(tmp_path):
    store = LiveStore(live_path(tmp_path))
    last_trade_id = add_closed_trade(store, T0 - 10, "-1.9789")
    store.disable(T0 - 2, OPERATOR_PAUSE)
    store.heartbeat(T0 - 1, CONFIGURED | {"pilot_budget": "12", "net_pnl": "-1.9789"})
    venue = Account()

    run, created = begin(store, venue)
    assert created and sorted(venue.reads) == ["buying_power", "orders", "positions"]
    assert run["starting_budget"] == "10" and run["baseline_realized_pnl"] == "-1.9789"
    assert run["last_closed_trade_id"] == last_trade_id and run["started_at"] == T0
    status = store.status()
    assert status["paused"] and status["pilot_run"] == run
    assert status["pilot_budget"] == status["pilot_equity"] == "10"
    assert status["pilot_net_pnl"] == "0" and status["net_pnl"] == "-1.9789"
    assert store.realized() == D("-1.9789")

    assert not store.resume_operator_pause(T0 + 1)
    store.heartbeat(T0 + 1, CONFIGURED | {"pilot_run_key": "oil-d-v2", "pilot_budget": "12"})
    assert not store.resume_operator_pause(T0 + 2)
    store.heartbeat(T0 + 2, CONFIGURED | {"pilot_run_key": "oil-d-v2", "pilot_budget": "10"})
    assert store.resume_operator_pause(T0 + 3)
    venue.reads.clear()
    assert begin(store, venue, at=T0 + 4) == (run, False)
    assert not venue.reads
    assert [event["kind"] for event in store.events()["events"]].count("pilot_started") == 1


@pytest.mark.parametrize(("venue", "configured", "error"), [
    (lambda: Account(funds="9.99"), "12", "buying power"),
    (lambda: Account(positions={"external": D(".01")}), "12", "open position or working order"),
    (lambda: Account(orders=(object(),)), "12", "open position or working order"),
    (Account, "9.99", "configured cap"),
])
def test_funding_configuration_and_account_exposure_blocks_new_reference(tmp_path, venue, configured, error):
    store = LiveStore(live_path(tmp_path))
    store.disable(T0 - 2, OPERATOR_PAUSE)
    store.heartbeat(T0 - 1, CONFIGURED)
    with pytest.raises(ValueError, match=error):
        begin(store, venue(), configured=configured)
    assert store.active_pilot() is None and store.status()["paused"]


def test_does_not_clear_an_unknown_pause_or_accept_stale_heartbeat(tmp_path):
    store = LiveStore(live_path(tmp_path))
    reason = "Exchange order submission could not be confirmed"
    store.disable(T0 - 2, reason)
    store.heartbeat(T0 - 1, CONFIGURED)
    account = Account()
    with pytest.raises(ValueError, match="reconciliation"):
        begin(store, account)
    assert not account.reads
    assert store.status()["disabled_reason"] == reason

    other = LiveStore(live_path(tmp_path / "other"))
    other.disable(T0 - 40, OPERATOR_PAUSE)
    other.heartbeat(T0 - 35, CONFIGURED)
    with pytest.raises(ValueError, match="current, configured"):
        begin(other, account)
    assert not account.reads


def test_operator_cannot_begin_next_pilot_with_active_journal_claim(tmp_path):
    store = LiveStore(live_path(tmp_path))
    signal = store.record_signal("window", T0 - 10, T0 + 890, T0 - 5, {})
    assert store.claim_entry(signal, "still-active", T0 - 4, {"side": "UP", "price": ".5", "quantity": "1"})
    store.disable(T0 - 2, OPERATOR_PAUSE)
    store.heartbeat(T0 - 1, CONFIGURED)
    account = Account()
    with pytest.raises(ValueError, match="active position or an unresolved order"):
        begin(store, account)
    assert not account.reads and store.active_trade() is not None


def test_new_pilot_preserves_lifetime_loss_and_halts_at_its_own_loss_limit(tmp_path):
    store = LiveStore(live_path(tmp_path))
    add_closed_trade(store, T0 - 12, "-2.00")
    store.disable(T0 - 2, PILOT_LOSS_PAUSE)
    store.heartbeat(T0 - 1, CONFIGURED | {"pilot_budget": "10"})
    account = Account()
    run, created = begin(store, account, configured="10")
    assert created and store.status()["paused"]
    assert store.status()["disabled_reason"].startswith("New entries stopped by ")

    now = [T0 + 1]
    restarted = Pilot(store, account, None, tmp_path, D("12"), clock=lambda: now[0])
    asyncio.run(restarted.tick())
    assert restarted.report["pilot_budget"] == "10"
    assert D(restarted.report["pilot_net_pnl"]) == 0
    assert D(restarted.report["pilot_equity"]) == 10
    store.heartbeat(now[0], CONFIGURED | restarted.report)
    assert store.resume_operator_pause(T0 + 2)
    now[0] = T0 + 3
    asyncio.run(restarted.tick())
    assert store.enabled() and restarted.report["net_pnl"] == "-2.00"

    old_signal = store.record_signal("late-prior-signal", T0 - 120, T0 + 780, T0 - .1, {})
    assert store.claim_entry(old_signal, "old-signal", T0 + 5, {"side": "UP", "price": ".5", "quantity": "1"}) is None

    add_closed_trade(store, T0 + 10, "-2.00")
    now[0] = T0 + 20
    asyncio.run(restarted.tick())
    assert restarted.report["net_pnl"] == "-4.00"
    assert restarted.report["pilot_net_pnl"] == "-2.00"
    assert D(restarted.report["pilot_equity"]) == 8
    assert store.status()["disabled_reason"] == PILOT_LOSS_PAUSE
    assert begin(store, account, at=now[0]) == (run, False)
    assert not store.resume_operator_pause(now[0])
