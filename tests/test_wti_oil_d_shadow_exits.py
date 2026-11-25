import asyncio
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

from backend.updown.feed import Settlement
from backend.updown.strategy import Book, Window
from backend.wti import oil_d_shadow_exits as exits
from backend.wti.oil_d_shadow_store import OilDShadowStore, oil_d_shadow_path
from backend.wti.tape import Tape, tape_path


WINDOW = Window("wti", "current-oil", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point",
                minimum_quantity="0.01", price_tick="0.01")


def scenario(directory):
    tape = Tape(tape_path(directory))
    tape.record_window(WINDOW)
    journal = OilDShadowStore(oil_d_shadow_path(directory))
    settings = SimpleNamespace(take_profit_target=.5, stop_loss=.5)
    context = exits.forward_context(WINDOW, settings, Decimal(".095"))
    journal.record("older-unmarked", 1000, "execution", 1002, {"outcome": "filled"})
    journal.record(WINDOW.slug, 1768, "execution", 1770, {
        "outcome": "filled", "forward_exits": context,
        "fill": {"side": "UP", "quantity": "1.00", "average_price": "0.40", "fee": "0.02",
                 "cost": "0.42", "filled_at": 1770},
    })
    return tape, journal


def put_book(tape, at, bid, source="stream"):
    book = Book(((bid, 100),), ((.85, 100),), at, at)
    tape.append(WINDOW.slug, at, source, "book", asdict(book))


def test_forward_comparison_uses_same_entry_and_waits_for_official_settlement(tmp_path, monkeypatch):
    tape, journal = scenario(tmp_path)
    for at in range(1770, 1796):
        put_book(tape, at, .17 if at <= 1773 else .44)

    assert exits.advance_exits(tmp_path, journal, 1803) == []
    assert exits.report(journal.path, 0, 1799)["status"] == {"window_open": 1}
    assert exits.advance_exits(tmp_path, journal, 1804) == [(WINDOW.slug, WINDOW.start)]
    waiting = exits.report(journal.path, 0, 1804)
    assert waiting["forward_shadow_entries"] == 1
    assert waiting["common_trades"] == 0
    assert waiting["status"] == {"pending_settlement": 1}
    assert waiting["policies"]["immediate_stop"]["modeled_trades"] == 1
    assert waiting["policies"]["confirmed_stop"]["modeled_trades"] == 1
    assert waiting["policies"]["target_then_settlement"]["pending_settlement"] == 1

    def fail_if_replayed(*args):
        raise AssertionError("Do not replay without new settlement evidence")

    with monkeypatch.context() as patch:
        patch.setattr(exits, "evaluate_entry", fail_if_replayed)
        assert exits.advance_exits(tmp_path, journal, 1819) == [(WINDOW.slug, WINDOW.start)]
    tape.record_settlement(Settlement(WINDOW.slug, 1, None, None), 1821)
    assert exits.advance_exits(tmp_path, journal, 1820) == [(WINDOW.slug, WINDOW.start)]
    assert exits.advance_exits(tmp_path, journal, 1821) == []
    final = exits.report(journal.path, 0, 1821, detail=True)
    assert final["status"] == {"modeled": 1}
    assert final["common_trades"] == 1
    assert final["common_entry_cost"] == Decimal("0.42")
    assert final["policies"]["immediate_stop"]["common_net"] == Decimal("-0.26")
    assert final["policies"]["confirmed_stop"]["common_net"] == Decimal("-0.26")
    assert final["policies"]["target_then_settlement"]["common_net"] == Decimal("0.58")
    cases = final["details"][0]["comparison"]["policies"]
    assert cases["confirmed_stop"]["stop_trigger_at"] - cases["immediate_stop"]["stop_trigger_at"] == 3
    assert cases["target_then_settlement"]["settled_quantity"] == "1.00"
    assert exits.advance_exits(tmp_path, journal, 1836) == []


def test_missing_stream_evidence_never_counts_rest_or_official_payout_as_a_comparable_trade(tmp_path):
    tape, journal = scenario(tmp_path)
    for at in range(1770, 1796):
        put_book(tape, at, .75, source="rest")
    tape.record_settlement(Settlement(WINDOW.slug, 1, None, None), 1804)
    assert exits.advance_exits(tmp_path, journal, 1810) == []
    result = exits.report(journal.path, 0, 1810)
    assert result["status"] == {"unverified": 1}
    assert result["unverified_reasons"] == {"no_usable_bid_frames": 1}
    assert result["common_trades"] == 0
    assert all(policy["unverified"] == 1 for policy in result["policies"].values())


def test_missing_official_payout_lookup_keeps_public_pacing_after_incomplete_payload(tmp_path, monkeypatch):
    tape, _ = scenario(tmp_path)
    at, calls = [1804], []
    stop = asyncio.Event()

    class FakeStop:
        def is_set(self):
            return stop.is_set()

        async def wait(self):
            at[0] += exits.CHECK_SECONDS
            raise TimeoutError

    class FixtureFeed:
        def __init__(self, venue, http):
            assert venue.priority == "background"
            assert venue.max_retries == 0

        async def settlement(self, product, start):
            assert (product.key, start) == ("wti-15m", WINDOW.start)
            calls.append(at[0])
            if len(calls) == 1:
                raise KeyError("assetPriceTerms")
            stop.set()
            return Settlement(WINDOW.slug, 0, None, None)

    monkeypatch.setattr(exits, "time", SimpleNamespace(time=lambda: at[0], monotonic=lambda: at[0]))
    monkeypatch.setattr(exits, "Feed", FixtureFeed)
    monkeypatch.setattr(exits, "advance_exits", lambda *args: [(WINDOW.slug, WINDOW.start)])
    asyncio.run(exits.run_forward_shadow_exits(tmp_path, FakeStop()))
    assert calls == [1804, 1864]
    assert exits.official_payout(tape.path, WINDOW.slug, WINDOW.start, WINDOW.end, 1864) == 0
