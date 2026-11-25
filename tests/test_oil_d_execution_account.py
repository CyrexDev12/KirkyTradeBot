import asyncio
from contextlib import closing
from decimal import Decimal
import json
import sqlite3
from dataclasses import replace

from backend.updown.strategy import Book, Window
from backend.wti.oil_d_paper import ENTRY_TRANSPORT_SECONDS, OilDPaper
from backend.wti.live_store import LiveStore, live_path
from backend.wti.oil_d_paper_store import PAPER_ENTRY_CAP, PaperAccount, events, paper_path, report
from backend.wti.oil_d_shadow_inputs import ShadowInputs, publish_shadow_inputs
from backend.wti.tape import Tape, tape_path


WINDOW = Window("wti", "wti-15m-first", 900, 1800, 80, "MARKET_STATUS_OPEN", .095, rule="point",
                minimum_quantity="0.01", price_tick="0.01")


def paper_store(directory, now=1000, *, paused=True):
    live = LiveStore(live_path(directory))
    if paused:
        live.disable(now - 10, "New entries stopped by fixture")
    return PaperAccount(paper_path(directory), now, live_journal=live.path)


def resume_live(store, now):
    live = LiveStore(store.live_journal)
    live.heartbeat(now, {"worker_configured": True, "entries_configured": True})
    assert live.resume_operator_pause(now)
    return live


def scenario(directory, *, paused=True):
    now, quotes, paid = [1000.0], [None, False], [None]
    Tape(tape_path(directory)).record_window(WINDOW)
    publish_shadow_inputs(directory, ShadowInputs(1000, 1000, 81.0, (80.0,) * 121, None, None))
    store = paper_store(directory, now[0], paused=paused)

    def snapshot(slug):
        assert slug == WINDOW.slug
        return tuple(quotes)

    async def settlement(slug, start, end, at):
        assert (slug, start, end) == (WINDOW.slug, 900, 1800) and at >= end
        return paid[0]

    def create_runner():
        runner = OilDPaper(directory, store, snapshot, settlement, clock=lambda: now[0])
        runner.signal_source.model.model_d = lambda *args: .95
        return runner

    def quote(at, bid=.46, ask=.50, size=100):
        now[0] = at
        quotes[:] = [Book(((bid, size),), ((ask, size),), at, at, sequence=round(at * 1000)), False]

    async def send(runner):
        quote(1000)
        await runner.tick()
        assert runner.pending is not None
        _, intent = runner.pending
        quote(intent.ready_at)
        await runner.tick()
        active = store.active()
        assert active["state"] == "sent"
        assert active["sent_at"] + ENTRY_TRANSPORT_SECONDS == active["payload"]["arrival_due"]
        assert Decimal("1.00") < Decimal(active["payload"]["reserved_amount"]) <= PAPER_ENTRY_CAP
        return active["payload"]["arrival_due"]

    return now, paid, store, create_runner, quote, send


def test_order_uses_arrival_depth_and_waits_for_official_residual_payout_after_restart(tmp_path):
    now, paid, store, create_runner, quote, send = scenario(tmp_path)

    async def exercise():
        runner = create_runner()
        arrival = await send(runner)
        quote(arrival - .01, ask=.48, size=.51)
        await runner.tick()
        assert store.active()["state"] == "sent"
        quote(arrival, ask=.49, size=.63)
        await runner.tick()
        active = store.active()
        fill = active["payload"]["entry"]
        assert active["state"] == "open" and fill["outcome"] == "partial"
        assert (fill["filled_quantity"], fill["average_price"], fill["arrival_at"]) == ("0.63", "0.49", arrival)
        assert report(store.path, 0, now[0])["buying_power"] < Decimal("100")

        quote(arrival + 1)
        await runner.tick()
        assert store.active()["payload"]["exit_events"][-1]["kind"] == "target_submitted"
        runner = create_runner()
        quote(arrival + 2, bid=.48, ask=.51)
        await runner.tick()
        assert store.active()["payload"]["exit_events"][-1]["kind"] == "target_working"

        now[0] = 1800
        await runner.tick()
        assert store.active()["state"] == "settling" and store.realized() == 0
        paid[0] = Decimal(1)
        now[0] += 1
        await runner.tick()
        assert store.active() is None
        expected = Decimal("0.63") - Decimal(fill["gross"]) - Decimal(fill["fee"])
        assert store.realized() == expected
        result = report(store.path, 0, now[0], detail=True)
        assert result["closed_in_period"] == 1 and result["closed_with_coverage_gap"] == 1
        assert result["trades"][0]["payload"]["exit_events"][-1]["kind"] == "settled"
        now[0] += 5
        await create_runner().tick()
        assert store.realized() == expected
        outbox = events(store.path)
        kinds = [item["kind"] for item in outbox["events"]]
        assert kinds.count("entry_sent") == kinds.count("entry_partial") == 1
        assert kinds.count("settlement") == kinds.count("trade_closed") == 1
        assert all("balance" in item and "realized_pnl" in item for item in outbox["events"])
        closed = next(item for item in outbox["events"] if item["kind"] == "trade_closed")
        assert Decimal(closed["net_pnl"]) == Decimal(closed["realized_pnl"]) == expected
        assert Decimal(closed["balance"]) == Decimal(100) + expected
        assert events(store.path, after=closed["id"])["events"] == []

    asyncio.run(exercise())


def test_zero_fill_and_interrupted_order_both_consume_the_window(tmp_path):
    for case in ("zero", "restart"):
        directory = tmp_path / case
        now, _, store, create_runner, quote, send = scenario(directory)

        async def exercise():
            runner = create_runner()
            arrival = await send(runner)
            quote(arrival, ask=.55)
            operator = create_runner() if case == "restart" else runner
            await operator.tick()
            assert store.active() is None and store.used(WINDOW.start)
            quote(arrival + 1, ask=.49)
            await operator.tick()
            assert operator.pending is None
            result = report(store.path, 0, now[0])
            assert result["entry_outcomes"] == ({"unverified": 1} if case == "restart" else {"no_fill": 1})
            assert result["balance"] == result["buying_power"] == Decimal("100")

        asyncio.run(exercise())


def test_delayed_entry_cannot_fill_after_its_signal_deadline(tmp_path):
    now, _, store, create_runner, quote, send = scenario(tmp_path)

    async def exercise():
        runner = create_runner()
        await send(runner)
        deadline = store.active()["payload"]["signal_deadline"]
        quote(deadline + .1, ask=.49)
        await runner.tick()

        assert store.active() is None and store.used(WINDOW.start)
        result = report(store.path, 0, now[0], detail=True)
        assert result["buying_power"] == Decimal("100.00")
        assert result["entry_outcomes"] == {"expired": 1}
        assert result["trades"][0]["payload"]["entry"]["reason"] == "order_after_cutoff"

    asyncio.run(exercise())


def test_loss_guard_and_closed_pnl_are_durable_and_idempotent(tmp_path):
    store = paper_store(tmp_path)
    path = store.path
    for index in range(2):
        window = replace(WINDOW, slug=f"wti-15m-{index}", start=900 * (index + 1), end=900 * (index + 2))
        now = window.start + 30
        assert not store.send(window, now - 2, now, {"reserved_amount": "10.01"})
        assert store.send(window, now - 2, now, {"reserved_amount": "10.00"})
        active = store.active()
        assert not store.send(replace(window, slug="duplicate"), now, now, {"reserved_amount": ".5"})
        assert store.update(active, now + .5, "open", {})
        opened = store.active()
        assert store.update(opened, now + 1, "closed", {"net_pnl": "-10.00"})
        assert not store.update(opened, now + 2, "closed", {"net_pnl": "-10.00"})
    assert PaperAccount(path, 3000, live_journal=store.live_journal).realized() == Decimal("-20")
    third = replace(WINDOW, slug="another", start=2700, end=3600)
    assert not store.send(third, 2800, 2802, {"reserved_amount": ".5"})
    store.heartbeat(3000, *store.entry_gate())
    result = report(path, 0, 3000)
    assert result["started_at"] == 1000 and result["loss_guard"] and result["balance"] == 80
    assert result["entry_cap"] == Decimal("10.00") and result["loss_cap"] == Decimal("20.00")
    assert not result["entries_allowed"] and result["worker_state"] == "loss_limit"


def test_enabled_live_blocks_paper_and_pause_resume_cancels_pending_order(tmp_path):
    now, _, store, create_runner, quote, send = scenario(tmp_path, paused=False)

    async def exercise():
        runner = create_runner()
        quote(1000)
        await runner.tick()
        waiting = report(store.path, 0, now[0])
        assert runner.pending is None and store.active() is None
        assert waiting["worker_state"] == "standby" and not waiting["entries_allowed"]
        live = LiveStore(store.live_journal)
        assert live.disable(1000, "New entries stopped by fixture")
        arrival = await send(runner)
        quote(arrival - .05)
        resume_live(store, now[0])
        await runner.tick()
        assert store.active() is None and store.used(WINDOW.start)
        quote(arrival)
        await runner.tick()
        result = report(store.path, 0, now[0])
        assert result["entry_outcomes"] == {"canceled": 1} and result["buying_power"] == Decimal(100)
        assert not result["entries_allowed"] and result["worker_reason"] == "live_entries_enabled"
        feed = events(store.path)["events"]
        assert [row["entries_allowed"] for row in feed if row["kind"] == "entry_gate"] == [False, True, False]
        canceled = next(row for row in feed if row["kind"] == "entry_unfilled")
        assert canceled["reason"] == "live_entries_enabled" and canceled["outcome"] == "canceled"

    asyncio.run(exercise())


def test_resume_and_re_pause_invalidates_old_simulated_order_at_commit(tmp_path):
    store = paper_store(tmp_path)
    assert store.send(WINDOW, 1000, 1002, {"side": "UP", "quantity": "1", "reserved_amount": ".5"})
    active = store.active()
    live = resume_live(store, 1002.05)
    assert live.disable(1002.07, "New entries stopped by fixture")
    assert store.update(active, 1002.15, "open", {**active["payload"], "entry": {"outcome": "filled"}})
    assert store.active() is None
    unfilled = events(store.path)["events"][-1]
    assert unfilled["kind"] == "entry_unfilled" and unfilled["reason"] == "live_pause_changed"


def test_filled_paper_position_settles_after_live_resumes(tmp_path):
    now, paid, store, create_runner, quote, send = scenario(tmp_path)

    async def exercise():
        runner = create_runner()
        arrival = await send(runner)
        quote(arrival)
        await runner.tick()
        assert store.active()["state"] == "open"
        resume_live(store, arrival + .2)
        quote(arrival + 1)
        await runner.tick()
        assert store.active()["state"] == "open"
        now[0] = 1801
        paid[0] = Decimal("1")
        await runner.tick()
        now[0] += 1  # A working take-profit must finish cancellation before settlement.
        await runner.tick()
        assert store.active() is None and store.realized() > 0
        assert not report(store.path, 0, now[0])["entries_allowed"]
        assert [row["kind"] for row in events(store.path)["events"]].count("settlement") == 1

    asyncio.run(exercise())


def test_allocation_change_preserves_legacy_history_and_loss_room(tmp_path):
    store = paper_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("DROP TABLE events")
        db.execute("DROP TABLE worker_status")
        db.execute("UPDATE account SET realized='-2.00' WHERE id=1")
        db.execute("""INSERT INTO trades VALUES('legacy-window',900,1800,1000,1002,1100,1100,
                      'closed',?)""", (json.dumps({"side": "UP", "net_pnl": "-2.00"}),))
    assert events(store.path) == {"events": [], "latest_id": 0}

    upgraded = PaperAccount(store.path, 3000, live_journal=store.live_journal)
    result = report(store.path, 0, 3000, detail=True)
    assert result["started_at"] == 1000 and result["realized_pnl"] == Decimal("-2.00")
    assert result["starting_budget"] == Decimal("100") and result["balance"] == Decimal("98.00")
    assert not result["loss_guard"]
    assert result["trades"][0]["slug"] == "legacy-window" and result["closed_in_period"] == 1
    assert events(store.path) == {"events": [], "latest_id": 0}
    next_window = replace(WINDOW, slug="new-window", start=1800, end=2700)
    assert not upgraded.send(next_window, 1900, 1902, {"reserved_amount": "10.01"})
    assert upgraded.send(next_window, 1900, 1902, {"reserved_amount": "10.00"})


def test_reserved_stake_shrinks_to_remaining_realized_loss_room(tmp_path):
    store = paper_store(tmp_path)
    for index in range(2):
        window = replace(WINDOW, slug=f"loss-{index}", start=900 * (index + 1), end=900 * (index + 2))
        at = window.start + 30
        assert store.send(window, at - 2, at, {"reserved_amount": "9.75"})
        assert store.update(store.active(), at + 1, "open", {})
        assert store.update(store.active(), at + 2, "closed", {"net_pnl": "-9.75"})
    assert store.realized() == Decimal("-19.50")
    next_window = replace(WINDOW, slug="next", start=2700, end=3600)
    assert not store.send(next_window, 2800, 2802, {"reserved_amount": ".51"})
    assert store.send(next_window, 2800, 2802, {"reserved_amount": ".50"})


def test_pause_gate_requires_readable_live_control_and_does_not_change_it(tmp_path):
    missing = tmp_path / "not-yet-configured.sqlite3"
    absent = PaperAccount(paper_path(tmp_path / "other"), 1000, live_journal=missing)
    assert absent.entry_gate() == (False, "live_control_unavailable")
    assert not absent.send(WINDOW, 1000, 1002, {"reserved_amount": ".50"}) and not missing.exists()

    store = paper_store(tmp_path)
    with closing(sqlite3.connect(store.live_journal)) as control:
        before = control.execute("SELECT disabled_at,disabled_reason FROM control").fetchone()
        control.execute("BEGIN IMMEDIATE")
        assert store.entry_gate() == (True, None)
        assert not store.send(WINDOW, 1000, 1002, {"reserved_amount": ".50"})
        control.rollback()
        assert store.send(WINDOW, 1000, 1002, {"reserved_amount": ".50"})
        assert control.execute("SELECT disabled_at,disabled_reason FROM control").fetchone() == before


def test_paused_live_journal_allows_paper_even_while_real_exits_are_open(tmp_path):
    store = paper_store(tmp_path, paused=False)
    live = LiveStore(store.live_journal)
    signal = live.record_signal(WINDOW.slug, WINDOW.start, WINDOW.end, 999, {"side": "UP"})
    assert live.claim_entry(signal, "real-order-fixture", 1000, {"price": ".50", "quantity": "1", "side": "UP"})
    live.disable(1001, "Pilot loss limit reached")
    assert live.active_trade() is not None and store.entry_gate() == (True, None)
    assert store.send(WINDOW, 1001, 1002, {"reserved_amount": ".50"})
