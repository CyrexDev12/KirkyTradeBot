import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path

import pytest

from backend.updown.feed import Settlement, parse_window
from backend.updown.strategy import Settings, Window, wti_products
from backend.wti import worker
from backend.wti.oil_d_shadow_inputs import ShadowInputs, publish_shadow_inputs, read_shadow_inputs, shadow_inputs_path
from backend.wti.replay_capture import Capture, current_window


WINDOW = Window("wti", "test-window", 900, 1800, None, "MARKET_STATUS_PREOPEN", .095, rule="point")


def test_atomic_reference_snapshot_rejects_invalid_inputs(tmp_path):
    directory = tmp_path / "wti"
    inputs = ShadowInputs(1200, 1198, 83.28, (83.0,) * 121, 1111, None)
    assert read_shadow_inputs(directory) is None
    publish_shadow_inputs(directory, inputs)
    assert read_shadow_inputs(directory) == inputs
    with pytest.raises(ValueError):
        publish_shadow_inputs(directory, replace(inputs, index_price=float("nan")))
    assert read_shadow_inputs(directory) == inputs
    path = shadow_inputs_path(directory)
    assert list(path.parent.iterdir()) == [path]
    path.write_text('{"minute_closes": "bad"}', encoding="utf-8")
    assert read_shadow_inputs(directory) is None


def test_wti_reference_published_from_existing_desk_reads_only_once_for_unchanged_inputs(monkeypatch, tmp_path):
    reads = []
    closes = tuple((1200 - (123 - index) * 60, 83.0 + index * .01) for index in range(124))

    class Feed:
        async def seconds(self, ids, _lookback):
            reads.append(("seconds", ids))
            return {"wti": [(1198, 84.25)]}

        async def minutes(self, ids, _lookback):
            reads.append(("minutes", ids))
            return {"wti": closes}

    desk = worker.Desk(Feed(), Settings(products=wti_products(("15m",))), models=("D",),
                       directory=tmp_path, clock=lambda: 1200)
    published = []

    def publish(directory, inputs):
        published.append(inputs)
        publish_shadow_inputs(directory, inputs)

    def detect(*_):
        desk.detector.shocks.append((1190, .01, 7))
        desk.detector.rolls.append((1150, .02))
        return []

    monkeypatch.setattr(worker, "publish_shadow_inputs", publish)
    monkeypatch.setattr(desk.detector, "update", detect)

    async def watch_twice():
        await desk.watch()
        await desk.watch()

    asyncio.run(watch_twice())
    assert reads == [("seconds", ["commodity:wti"]), ("minutes", ["commodity:wti"])]
    expected = ShadowInputs(1200, 1198, 84.25, tuple(price for _, price in closes[-121:]), 1190, 1150)
    assert published == [expected]
    assert read_shadow_inputs(tmp_path) == expected


def test_window_capture_refreshes_metadata_without_restarting_or_changing_settlement(monkeypatch, tmp_path):
    capture = Capture(tmp_path, None, clock=lambda: 1200)
    seen = []
    write = capture.tape.record_window

    async def no_network(_):
        return

    def record(window):
        seen.append(window)
        write(window)

    monkeypatch.setattr(capture.tape, "record_window", record)
    monkeypatch.setattr(capture, "_capture", no_network)

    async def update():
        capture.window(WINDOW)
        first_task = capture.tasks[WINDOW.slug]
        assert current_window(capture.tape.path, 1200) == WINDOW
        update = replace(WINDOW, status="MARKET_STATUS_OPEN", price_to_beat=84.15,
                         price_tick="0.01", minimum_quantity="0.01")
        capture.window(update)
        capture.window(update)
        assert capture.tasks[WINDOW.slug] is first_task
        assert seen == [WINDOW, update]
        assert current_window(capture.tape.path, 1200) == update
        capture.tape.record_settlement(Settlement(WINDOW.slug, 1, 84.15, 84.3), 1810)
        capture.tape.record_window(WINDOW)
        with closing(capture.tape.connect()) as db:
            assert db.execute("SELECT payout,settled_at FROM markets").fetchone() == (1, 1810)
        assert current_window(capture.tape.path, 1200) == update
        await capture.close()

    asyncio.run(update())


def test_parse_window_preserves_exact_tick_and_minimum_from_public_metadata():
    capture = Path(__file__).with_name("fixtures") / "wti_capture_2026-11-22.json"
    fixture = json.loads(capture.read_text(encoding="utf-8"))
    window = parse_window("wti", fixture["event_15m"], rule="point")
    assert (window.price_tick, window.minimum_quantity) == ("0.01", "0.01")
    legacy = parse_window("wti", fixture["event_1d"], rule="point")
    assert legacy.price_tick is None and legacy.minimum_quantity is None
    market = fixture["event_15m"]["events"][0]["markets"][0]
    market.update(orderPriceMinTickSize="0.00250", minimumTradeQty="0.0100")
    precise = parse_window("wti", fixture["event_15m"], rule="point")
    assert (precise.price_tick, precise.minimum_quantity) == ("0.00250", "0.0100")
    market.update(orderPriceMinTickSize=1, minimumTradeQty=True)
    invalid = parse_window("wti", fixture["event_15m"], rule="point")
    assert invalid.price_tick is None and invalid.minimum_quantity is None
