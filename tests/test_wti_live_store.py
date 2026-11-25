from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from backend.wti.live_store import LiveStore, live_path


ENTRY = {"side": "UP", "quantity": Decimal("2.5"), "price": Decimal("0.405")}
EXIT = {"side": "UP", "quantity": "1.5", "price": "0.52"}


def signal(store, at, start=1000):
    return store.record_signal(f"wti-15m-{start}", start, start + 900, at, {"side": "UP", "ready_at": at + 2})


def test_live_pause_survives_restart_and_preserves_exit_management(tmp_path):
    store = LiveStore(live_path(tmp_path))
    intent = signal(store, 1001.5)
    assert signal(store, 1001.5) == intent
    entry = store.claim_entry(intent, "entry-1", 1004.5, ENTRY)
    trade_id = entry["trade_id"]
    assert entry["state"] == "prepared" and entry["request"]["price"] == "0.405"

    assert store.disable(1005.0, "operator Luke")
    store = LiveStore(live_path(tmp_path))
    assert not store.enabled() and not store.disable(1006.0)
    store.heartbeat(1006.5, {"state": "blocked", "message": "Waiting for the exchange", "position": "1.5"})
    status = store.status()
    assert status["state"] == "paused" and status["position"] == "1.5"
    assert status["worker_state"] == "blocked" and status["worker_message"] == "Waiting for the exchange"
    assert status["last_heartbeat"] == "1970-01-01T00:16:46.500000+00:00"
    assert status["disabled_reason"] == "operator Luke"
    assert status["active_trade"]["id"] == trade_id
    assert store.set_trade(trade_id, 1007.0, {"stop_triggered": True})
    assert store.prepare_order(trade_id, "stop", "exit-1", 1008.0, EXIT)["state"] == "prepared"
    store.close_trade(trade_id, 1010.0, "closed", {"net_pnl": Decimal("-0.06")})
    assert store.active_trade() is None
    assert store.trades()[0]["payload"] == {"net_pnl": "-0.06", "stop_triggered": True}
    assert store.claim_entry(signal(store, 2004.0, 1900), "entry-2", 2006.0, ENTRY) is None
    assert [event["kind"] for event in store.events()["events"]] == [
        "order_prepared", "paused", "order_prepared", "trade_closed",
    ]


def test_entry_authorizes_once_or_cancels_if_pause_wins(tmp_path):
    store = LiveStore(live_path(tmp_path))
    first = store.claim_entry(signal(store, 1001), "entry-first", 1003, ENTRY)
    assert store.authorize_entry(first["id"], 1004)
    assert not store.authorize_entry(first["id"], 1005)
    assert store.orders(first["trade_id"])[0]["state"] == "sending"
    # Complete the order lifecycle before preparing the next window.
    store.update_order(first["id"], "ORDER_STATE_REJECTED", 1006)
    store.close_trade(first["trade_id"], 1006, "NO_FILL")
    second = store.claim_entry(signal(store, 1901, 1900), "entry-second", 1903, ENTRY)
    assert store.disable(1903.5)
    assert not store.authorize_entry(second["id"], 1904)
    canceled = store.orders(second["trade_id"])[0]
    assert canceled["state"] == "ORDER_STATE_CANCELED"
    assert canceled["venue_id"] is None and canceled["payload"]["filled"] == "0"
    latest_id = store.events()["latest_id"]
    assert not store.authorize_entry(second["id"], 1905)
    assert store.events()["latest_id"] == latest_id


def test_entry_authorization_rechecks_signal_deadline(tmp_path):
    store = LiveStore(live_path(tmp_path))
    intent = store.record_signal("wti-15m-1000", 1000, 1900, 1001, {"deadline": 1005})
    order = store.claim_entry(intent, "entry-1", 1003, ENTRY)
    assert not store.authorize_entry(order["id"], 1005)
    event = store.events()["events"][-1]
    assert event["state"] == "ORDER_STATE_CANCELED" and event["quantity"] == "0"
    assert event["message"] == "The entry deadline passed."


def test_live_order_cumulative_state_and_execution_ids_are_durable(tmp_path):
    store = LiveStore(live_path(tmp_path))
    entry = store.claim_entry(signal(store, 1001), "entry-1", 1003, ENTRY)
    order_id = entry["id"]
    current = store.update_order(order_id, "partially_filled", 1004, "venue-1",
                                 {"filled": "1.25", "average_price": Decimal("0.39"), "fees": Decimal("0.005")})
    assert store.record_fill(order_id, "execution-1", 1004, "1.25", "0.39", "0.005")
    assert not store.record_fill(order_id, "execution-1", 1004, "1.25", "0.39", "0.005")
    assert store.update_order(order_id, "new", 1005, payload={"filled": "0"}) == current
    store.update_order(order_id, "canceled", 1006, payload={"filled": None, "fees": None})
    store = LiveStore(live_path(tmp_path))
    final = store.orders(entry["trade_id"])[0]
    assert final["state"] == "canceled" and final["venue_id"] == "venue-1"
    assert final["payload"] == {"average_price": "0.39", "fees": "0.005", "filled": "1.25"}
    assert len(store.fills(entry["trade_id"])) == 1
    assert len(store.events(after=1)["events"]) == 3


def test_cumulative_fill_without_total_price_clears_stale_execution_values(tmp_path):
    store = LiveStore(live_path(tmp_path))
    entry = store.claim_entry(signal(store, 1001), "entry-1", 1003, ENTRY)
    order_id = entry["id"]
    initial = {"filled": "1.25", "average_price": "0.39", "fees": "0.005"}
    store.update_order(order_id, "ORDER_STATE_PARTIALLY_FILLED", 1004, "venue-1", initial)
    latest_id = store.events()["latest_id"]
    store.update_order(order_id, "ORDER_STATE_PARTIALLY_FILLED", 1005,
                       payload={"filled": "1.250", "cancel_requested_at": 1005})
    assert store.events()["latest_id"] == latest_id

    updated = store.update_order(order_id, "ORDER_STATE_FILLED", 1006,
                                 payload={"filled": "2.5", "average_price": None, "fees": None})
    assert updated["payload"] == {"filled": "2.5", "cancel_requested_at": 1005}
    recovered = store.update_order(order_id, "ORDER_STATE_FILLED", 1007,
                                   payload={"average_price": "0.40", "fees": "0.01"})
    assert recovered["payload"]["average_price"] == "0.40"
    assert recovered["payload"]["fees"] == "0.01"


def test_live_account_has_one_active_entry_and_one_entry_per_window(tmp_path):
    store = LiveStore(live_path(tmp_path))
    signals = [signal(store, 1001), signal(store, 1001.1)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda intent: store.claim_entry(intent, f"entry-{intent}", 1003, ENTRY), signals))
    trade, = store.trades()
    assert sum(order is not None for order in attempts) == 1
    next_window_signal = signal(store, 1901, 1900)
    assert store.claim_entry(next_window_signal, "next-window", 1903, ENTRY) is None
    store.close_trade(trade["id"], 1005, "closed")
    assert store.claim_entry(signal(store, 1006), "duplicate-window", 1008, ENTRY) is None
    assert store.claim_entry(next_window_signal, "next-window", 1903, ENTRY) is not None
