from decimal import Decimal
from types import SimpleNamespace

from backend.updown.strategy import Window
from backend.wti.live_exit_replay_data import load_live_trades, load_tape_markets
from backend.wti.live_store import LiveStore
from backend.wti.tape import Tape


def confirmed_trade(store, slug, start, *, source="stream", quote_source="stream", size="2.25"):
    signal = store.record_signal(slug, start, start + 900, start + 31,
                                 {"side": "DOWN", "deadline": start + 810, "signal_source": source})
    order = store.claim_entry(signal, f"{slug}-entry", start + 33,
                              {"side": "DOWN", "price": ".41", "quantity": size, "signal_source": source,
                               "quote_source": quote_source})
    store.authorize_entry(order["id"], start + 33.4)
    store.update_order(order["id"], "ORDER_STATE_FILLED", start + 34, venue_id=slug,
                       payload={"filled": size, "average_price": ".4", "fees": ".05"})
    return order


def test_loads_only_confirmed_stream_entries_and_preserves_partial_exit_ledger(tmp_path):
    store = LiveStore(tmp_path / "live.sqlite3")
    entry = confirmed_trade(store, "oil-one", 900)
    store.record_fill(entry["id"], "receipt1", 933.7, "1", ".4", ".02",
                      {"exchange_at": 933.7, "observed_at": 934})
    store.record_fill(entry["id"], "receipt2", 934, "1.25", ".4", ".03", {"observed_at": 934})
    target = store.prepare_order(entry["trade_id"], "target", "target-a", 940,
                                 {"side": "DOWN", "price": ".8", "quantity": "2.25", "expires_at": 1795})
    store.update_order(target["id"], "sending", 940.125)
    store.update_order(target["id"], "ORDER_STATE_CANCELED", 1800, venue_id="target",
                       payload={"filled": "1.10", "average_price": ".8", "cancel_requested_at": 1796})
    store.record_fill(target["id"], "receipt3", 1000, "1.10", ".8", ".02", {"exchange_at": 1000})
    store.close_trade(entry["trade_id"], 1802, "CLOSED",
                      {"net_pnl": "1.062000", "fees_exact": False, "fees": ".068",
                       "payout": "1.15", "exit_reason": "settlement"})
    paper = confirmed_trade(store, "oil-paper", 1800, source="paper")
    store.close_trade(paper["trade_id"], 2701, "CLOSED", {"net_pnl": "0.1"})
    rest = confirmed_trade(store, "oil-rest", 2700, quote_source="recorded_rest")
    store.close_trade(rest["trade_id"], 3601, "CLOSED", {"net_pnl": "0.1"})
    confirmed_trade(store, "oil-active", 3600)

    trade, = load_live_trades(store.path, 0, 5000)
    assert trade.slug == "oil-one" and trade.side == "DOWN" and trade.closed_at == 1802
    assert trade.net_pnl == Decimal("1.062") and trade.fees == Decimal(".068") and trade.fees_exact is False
    assert trade.exit_reason == "settlement" and trade.payout_total == Decimal("1.15")
    assert trade.remaining_quantity == Decimal("1.15")
    assert trade.entry.prepared_at == 933 and trade.entry.sending_at == 933.4
    assert trade.entry.first_exchange_fill_at == trade.entry.last_exchange_fill_at == 933.7
    assert trade.entry.fills[-1].exchange_at is None and trade.entry.fills[-1].observed_at == 934
    assert trade.entry.requested_quantity == trade.entry.filled_quantity == Decimal("2.25")
    assert trade.entry.average_price == Decimal(".4") and trade.entry.fees == Decimal(".05")
    target, = trade.exits
    assert target.kind == "target" and target.state == "ORDER_STATE_CANCELED" and target.sending_at == 940.125
    assert target.filled_quantity == Decimal("1.10") and target.fees is None
    assert target.limit_price == Decimal(".8") and target.first_exchange_fill_at == 1000
    assert target.cancel_requested_at == 1796 and target.expires_at == 1795
    assert load_live_trades(store.path, 0, 1801) == []


def test_uses_only_same_window_official_payout_known_after_market_close(tmp_path):
    tape = Tape(tmp_path / "tape.sqlite3")
    trade = SimpleNamespace(slug="oil-one", window_start=900, window_end=1800)
    window = Window("wti", trade.slug, 900, 1800, None, "MARKET_STATUS_OPEN", .095,
                    minimum_quantity=".01", price_tick=".01")
    tape.record_window(window)
    tape.record_settlement(SimpleNamespace(slug=trade.slug, yes_payout=0), 1810)
    mismatched = SimpleNamespace(slug=trade.slug, window_start=0, window_end=900)

    unknown = load_tape_markets(tape.path, [trade], as_of=1805)[trade.slug]
    assert unknown.yes_payout is unknown.settled_at is None
    assert unknown.price_tick == unknown.minimum_quantity == Decimal(".01")
    known = load_tape_markets(tape.path, [trade], as_of=1810)[trade.slug]
    assert known.yes_payout == Decimal(0) and known.settled_at == 1810
    assert load_tape_markets(tape.path, [mismatched], as_of=1811) == {}

    invalid = SimpleNamespace(slug="premature", window_start=900, window_end=1800)
    tape.record_window(Window("wti", invalid.slug, 900, 1800, None, "MARKET_STATUS_OPEN", .095))
    tape.record_settlement(SimpleNamespace(slug=invalid.slug, yes_payout=1), 1799)
    assert load_tape_markets(tape.path, [invalid], as_of=1850)[invalid.slug].yes_payout is None


def test_missing_replay_journals_stay_missing(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    assert load_live_trades(missing, 0, 1000) == []
    assert load_tape_markets(missing, [SimpleNamespace(slug="oil-one")], as_of=1000) == {}
    assert not missing.exists()
