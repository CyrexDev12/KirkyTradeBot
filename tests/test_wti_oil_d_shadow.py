import asyncio
from dataclasses import asdict
from decimal import Decimal

from backend.updown.strategy import Book, Window
from backend.wti.oil_d_shadow import OilDShadow
from backend.wti.oil_d_shadow_inputs import ShadowInputs, publish_shadow_inputs
from backend.wti.oil_d_shadow_store import OilDShadowStore, oil_d_shadow_path, report
from backend.wti.replay_capture import StreamBookSource
from backend.wti.tape import Tape, tape_path


WINDOW = Window("wti", "current-oil", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point",
                minimum_quantity="0.01", price_tick="0.01")


def scenario(directory):
    now = [1000.0]
    clock = lambda: now[0]
    tape = Tape(tape_path(directory))
    tape.record_window(WINDOW)
    tape.append(WINDOW.slug, 999, "intent", "order", {"model": "D", "action": "buy", "side": "UP",
                                                      "limit": .48, "fair_up": .9, "theta": .095, "min_edge": .02})
    publish_shadow_inputs(directory, ShadowInputs(now[0], now[0], 81.0, (80.0,) * 121, None, None))
    source = StreamBookSource(clock)
    generation = source.begin(WINDOW)
    source.set_connected(generation, True)
    store = OilDShadowStore(oil_d_shadow_path(directory))
    runner = OilDShadow(directory, store, source.snapshot, clock)
    return now, tape, source, generation, store, runner


def publish(source, generation, now, ask, sequence, age=0, size=100):
    book = Book(((.46, 100),), ((ask, size),), now, now - age, sequence=sequence)
    source.observe(generation, (WINDOW.slug, now, "stream", "book", asdict(book)))


def test_oil_d_uses_stream_for_decision_and_fills_after_latency_without_reuse(tmp_path):
    now, tape, source, generation, store, runner = scenario(tmp_path)

    async def run():
        publish(source, generation, now[0], .50, 1)
        await runner.scan()
        assert runner.pending is not None
        intent = runner.pending.intent
        assert intent.limit == Decimal(".50")

        now[0] = intent.ready_at
        tape.append(WINDOW.slug, now[0], "rest", "book",
                    asdict(Book(((.46, 100),), ((.48, 100),), now[0], now[0], now[0])))
        await runner.execute()
        assert runner.pending is not None and not store.has_fill(WINDOW.slug)

        publish(source, generation, now[0], .52, 2)
        await runner.execute()
        assert runner.pending.last_quote["reason"] == "price_above_limit"
        now[0] += .2
        publish(source, generation, now[0], .49, 3)
        await runner.execute()
        assert runner.pending is None and store.has_fill(WINDOW.slug)

        restarted = OilDShadow(tmp_path, store, source.snapshot, runner.clock)
        await restarted.scan()
        assert restarted.pending is None

    asyncio.run(run())
    rows = report(oil_d_shadow_path(tmp_path), 900, now[0])["recent"]
    entries = {row["stage"]: row["payload"] for row in rows}
    assert entries["signal"]["original_limit"] == .48
    assert entries["signal"]["best_ask"] == .50
    assert entries["signal"]["book_sequence"] == 1
    execution = entries["execution"]
    assert execution["source"] == "stream" and execution["book_sequence"] == 3
    assert execution["best_ask"] == .49 and execution["net_edge"] > .02
    assert execution["outcome"] == "filled"
    assert Decimal(execution["fill"]["cost"]) <= Decimal("1.00")
    assert Decimal(execution["fill"]["limit"]) == Decimal("0.50")
    assert execution["forward_exits"] == {"version": 1, "window_start": WINDOW.start, "window_end": WINDOW.end,
                                          "minimum_quantity": "0.01", "price_tick": "0.01",
                                          "fee_coefficient": "0.095", "take_profit_fraction": .5,
                                          "stop_loss_fraction": .5}


def test_old_exchange_book_cannot_generate_signal_and_halt_cannot_fill(tmp_path):
    now, _, source, generation, store, runner = scenario(tmp_path)

    async def run():
        publish(source, generation, now[0], .50, 1, age=16)
        await runner.scan()
        assert runner.pending is None
        publish(source, generation, now[0], .50, 2, age=15)
        await runner.scan()
        assert runner.pending is not None
        now[0] = runner.pending.intent.ready_at
        source.observe(generation, (WINDOW.slug, now[0], "stream", "closed", {"exchange_at": now[0]}))
        await runner.execute()
        assert runner.pending is None
        assert not store.has_fill(WINDOW.slug)

    asyncio.run(run())
    rows = report(oil_d_shadow_path(tmp_path), 900, now[0])["recent"]
    skip = next(row for row in rows if row["stage"] == "execution")
    assert skip["payload"]["reason"] == "market_not_open"
