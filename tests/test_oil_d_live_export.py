from backend.wti.live_store import LiveStore
from backend.wti.oil_d_shadow_store import OilDShadowStore
from deploy.oil_d_live_export import (ATTEMPT_FIELDS, LIVE_FIELDS, SHADOW_FIELDS,
                                      live_attempt_rows, live_rows, shadow_rows)


def add_live_entry(store, slug, start, *, source=None):
    at = start + 15.125
    signal = {"side": "DOWN", "ready_at": at + 2, "deadline": start + 800}
    request = {"side": "DOWN", "quantity": "2.0", "price": "0.40", "signal_at": at}
    if source:
        signal.update(signal_source=source, signal_book_received_at=at - .225, signal_book_exchange_at=at - .5)
        request.update(signal_source=source, quote_source=source)
    signal_id = store.record_signal(slug, start, start + 900, at, signal)
    return at, store.claim_entry(signal_id, f"private-idempotency-{slug}", at + 3, request)


def test_live_rows_keep_realized_zero_fill_and_unconfirmed_entries_distinct(tmp_path):
    path = tmp_path / "live.sqlite3"
    store = LiveStore(path)
    at, order = add_live_entry(store, "closed-market", 900, source="stream")
    assert store.authorize_entry(order["id"], at + 3.1)
    store.update_order(order["id"], "ORDER_STATE_FILLED", at + 4, venue_id="exchange-closed",
                       payload={"filled": "2.0", "average_price": "0.39", "fees": ".03"})
    store.record_fill(order["id"], "exec-a", at + 3.5, "1", ".38", ".01")
    store.record_fill(order["id"], "exec-b", at + 3.6, "1", ".40", ".02", {"exchange_at": at + 3.6})
    exit_order = store.prepare_order(order["trade_id"], "target", "exit-key", at + 8,
                                     {"side": "DOWN", "quantity": "2", "price": ".80"})
    store.record_fill(exit_order["id"], "exec-exit", at + 8.25, "2", ".8", ".01")
    store.close_trade(order["trade_id"], at + 9, "CLOSED",
                      {"net_pnl": ".78", "fees": ".04", "fees_exact": False, "exit_reason": "target"})
    store.record_entry_timing(at + 4, {"slug": "closed-market", "signal_at": at, "outcome": "submitted",
                                      "submit_started_at": at + 3.25, "final_quote_source": "stream",
                                      "book_mode": "stream_end_to_end", "acknowledged": True, "filled": "2.0"})
    store.record_entry_timing(at + 5, {"slug": "closed-market", "signal_at": at, "outcome": "skipped"})

    no_fill_at, no_fill = add_live_entry(store, "empty-market", 1800)
    store.update_order(no_fill["id"], "ORDER_STATE_CANCELED", no_fill_at + 4, venue_id="exchange-empty",
                       payload={"filled": "0"})
    store.close_trade(no_fill["trade_id"], no_fill_at + 4, "NO_FILL", {"message": "No fill"})

    unknown_at, unknown = add_live_entry(store, "unclear-market", 2700, source="stream")
    store.update_order(unknown["id"], "UNKNOWN", unknown_at + 4)

    entries = live_rows(path)
    assert [row["market_slug"] for row in entries] == ["unclear-market", "empty-market", "closed-market"]
    unknown, empty, closed = entries
    assert tuple(closed) == LIVE_FIELDS
    assert unknown["order_state"] == "UNKNOWN" and unknown["filled_quantity"] is None
    assert empty["order_state"] == "ORDER_STATE_CANCELED" and empty["filled_quantity"] == "0"
    assert unknown["net_pnl_usd"] is empty["net_pnl_usd"] is None
    assert empty["reason"] == "No fill" and empty["signal_source"] == "paper"
    assert closed["order_submitted_at"] == at + 3.25 and closed["quote_source"] == "stream"
    assert closed["signal_book_received_at"] == at - .225
    assert closed["signal_book_exchange_at"] == at - .5
    assert (closed["first_fill_at"], closed["last_fill_at"], closed["recorded_fill_count"]) == (at + 3.5, at + 3.6, 2)
    assert closed["first_fill_exchange_at"] == closed["last_fill_exchange_at"] == at + 3.6
    assert closed["filled_quantity"] == "2.0" and closed["fill_price"] == "0.39"
    assert closed["net_pnl_usd"] == ".78" and closed["fees_exact"] is False
    assert closed["total_fees_usd"] == ".04" and closed["exit_reason"] == "target"
    assert not any("private-idempotency" in str(row) for row in entries)

    earlier, = live_rows(path, as_of=at + 5)
    assert earlier["trade_state"] == "active" and earlier["closed_at"] is earlier["net_pnl_usd"] is None


def test_live_attempt_rows_include_skips_without_an_order(tmp_path):
    path = tmp_path / "live.sqlite3"
    store = LiveStore(path)
    store.record_entry_timing(108, {"slug": "m", "signal_at": 100.2, "signal_source": "stream", "side": "UP",
                                    "outcome": "skipped", "reason": "price_above_limit", "message": "Offer was high",
                                    "limit": "0.45", "ready_at": 102.2, "checks_completed_at": 105})
    assert live_rows(path) == []
    assert live_attempt_rows(path, as_of=107.99) == []
    result, = live_attempt_rows(path)
    assert tuple(result) == ATTEMPT_FIELDS
    assert result["market_slug"] == "m" and result["attempt_at"] == 108
    assert result["limit_price"] == "0.45" and result["checks_completed_at"] == 105
    assert result["order_submitted_at"] is None and result["reason"] == "price_above_limit"


def test_shadow_rows_pair_signals_with_fill_evidence_and_omit_evaluations(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    store = OilDShadowStore(path)
    store.record("filled-market", 100, "signal", 100.1, {
        "side": "UP", "outcome": "signaled", "source": "stream", "best_ask": .37, "net_edge": .06,
        "book_received_at": 99.6, "book_exchange_at": 99.2, "original_signal_at": 98.125, "original_limit": .36,
        "shadow_limit": .38, "quantity": "3", "ready_at": 102, "deadline": 108,
    })
    store.record("filled-market", 100, "execution", 104, {
        "outcome": "filled", "source": "stream", "best_ask": .38, "net_edge": .05,
        "book_received_at": 103.5, "book_exchange_at": 103.1,
        "fill": {"quantity": "2", "limit": ".38", "average_price": ".38", "fee": ".045", "cost": ".805",
                 "filled_at": 103.8},
    })
    store.record("skipped-market", 115, "signal", 115.2, {"side": "DOWN", "outcome": "signaled"})
    store.record("skipped-market", 115, "execution", 120, {"outcome": "skipped", "reason": "insufficient_depth"})
    store.record("pending-market", 130, "signal", 130.2, {"side": "UP", "outcome": "signaled"})
    store.record("unrelated-evaluation", 130, "evaluation", 130.3, {"outcome": "skipped"})
    pending, skipped, filled = shadow_rows(path)
    assert tuple(filled) == SHADOW_FIELDS
    assert pending["market_slug"] == "pending-market" and pending["execution_at"] is None
    assert skipped["outcome"] == "skipped" and skipped["reason"] == "insufficient_depth"
    assert filled["signal_book_received_at"] == 99.6 and filled["signal_book_exchange_at"] == 99.2
    assert filled["signal_best_ask"] == .37 and filled["best_ask"] == .38
    assert filled["requested_quantity"] == "3" and filled["filled_quantity"] == "2"
    assert filled["first_fill_at"] == 103.8 and filled["fill_price"] == ".38"
    assert filled["net_pnl_usd"] is filled["fees_exact"] is None

    old_signal, = shadow_rows(path, as_of=103.99)
    assert old_signal["outcome"] == "signaled" and old_signal["first_fill_at"] is None


def test_readers_leave_missing_optional_journals_absent(tmp_path):
    path = tmp_path / "absent.sqlite3"
    assert live_rows(path) == live_attempt_rows(path) == shadow_rows(path) == []
    assert not path.exists()
