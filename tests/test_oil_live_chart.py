import asyncio
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from backend.wti.live_chart import chart
from backend.wti.live_store import LiveStore, live_path


def add_trade(store, at, pnl=None, *, exact=True):
    request = {"side": "UP", "quantity": "1", "price": "0.40"}
    intent = store.record_signal(f"wti-{at}", at, at + 900, at, {"side": "UP", "ready_at": at})
    order = store.claim_entry(intent, f"entry-{at}", at, request)
    if pnl is not None:
        store.update_order(order["id"], "ORDER_STATE_FILLED", at + 1, payload={"filled": "1"})
        store.close_trade(order["trade_id"], at + 30, "CLOSED", {"net_pnl": pnl, "fees_exact": exact})
    return order["trade_id"]


def test_legacy_chart_keeps_opening_pnl_and_excludes_open_position(tmp_path):
    path = live_path(tmp_path)
    now = 1_790_000_000
    store = LiveStore(path)
    store.heartbeat(now, {"pilot_budget": "12"})
    add_trade(store, now - 90_000, "-0.75")
    add_trade(store, now - 3_000, "0.20", exact=False)
    add_trade(store, now - 1_000, "-0.01")
    add_trade(store, now - 300)
    data = chart(path, "24h", now)
    assert [p["balance"] for p in data["points"]] == ["11.25", "11.45", "11.44", "11.44"]
    assert data["closed_in_range"] == 2 and data["estimated_fees_count"] == 1 and data["open_trade"]
    assert data["range_pnl"] == "0.19" and data["pilot_pnl"] == "-0.56"
    assert data["budget_source"] == "legacy_heartbeat"
    from bot.graphs import oil_live_graph
    assert oil_live_graph(data).read(8) == b"\x89PNG\r\n\x1a\n"


def test_empty_chart_and_private_endpoint(tmp_path, monkeypatch):
    from backend.app import app
    monkeypatch.setenv("NFL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NFL_PAPER_OPERATOR_TOKEN", "local-test-token")
    monkeypatch.delenv("NFL_PAPER_OPERATOR_TOKEN_FILE", raising=False)
    headers = {"X-Paper-Operator-Token": "local-test-token"}
    path = live_path(tmp_path / "wti")
    with closing(TestClient(app)) as client:
        assert client.get("/api/wti/live/chart").status_code == 403
        assert client.get("/api/wti/live/chart", headers=headers).json()["available"] is False
        assert not path.exists()
        assert client.get("/api/wti/live/chart?time=bogus", headers=headers).status_code == 422
        store = LiveStore(path)
        store.heartbeat(1_790_000_000, {"pilot_budget": "12"})
        result = client.get("/api/wti/live/chart?time=24h", headers=headers).json()
        assert result["available"] and result["closed_in_range"] == 0
        assert [p["balance"] for p in result["points"]] == ["12", "12"]


def test_chart_respects_new_pilot_after_legacy_losses(tmp_path):
    from bot.oil_live import status_card
    now = 1_790_000_000
    path = live_path(tmp_path)
    journal = LiveStore(path)
    add_trade(journal, now - 90_000, "-1.98")
    journal.disable(now - 2001, "New entries stopped by operator.")
    journal.heartbeat(now - 2001, {"pilot_budget": "12", "worker_configured": True, "entries_configured": True})
    run, _ = journal.start_pilot("ten-dollar-pilot", "10", now - 2000, "10")
    journal.heartbeat(now - 1999, {"pilot_budget": "10", "worker_configured": True, "entries_configured": True,
                                  "pilot_run_key": run["pilot_key"]})
    assert journal.resume_operator_pause(now - 1998)
    add_trade(journal, now - 1000, "-0.08")
    data = chart(path, "24h", now)
    assert data["budget_source"] == "pilot_run"
    assert [p["balance"] for p in data["points"]] == ["10", "9.92", "9.92"]
    assert data["range_pnl"] == data["pilot_pnl"] == "-0.08" and data["closed_in_range"] == 1
    card = status_card({"pilot_run": run, "pilot_net_pnl": "-0.08", "pilot_equity": "9.92", "net_pnl": "-2.06"}, None)
    assert dict(card["fields"])["Lifetime live net P&L"] == "-$2.06"
    assert "Realized balance $9.92" in dict(card["fields"])["Current pilot"]


def test_discord_chart_fetches_requested_window_and_returns_private_attachment(tmp_path):
    from bot.discord_bot import NFLBot
    from bot.storage import Store
    journal = LiveStore(live_path(tmp_path))
    journal.heartbeat(10_000, {"pilot_budget": "12"})
    async def run():
        api = SimpleNamespace(oil_live=AsyncMock(return_value=chart(journal.path, "24h", 20_000)))
        bot = NFLBot(api, Store(tmp_path / "discord.db"), 12, 123)
        interaction = SimpleNamespace(response=AsyncMock(), followup=AsyncMock())
        cmd = bot.tree.get_command("oil-live").get_command("chart")
        await cmd.callback(interaction, "24h")
        api.oil_live.assert_awaited_once_with("/chart", time="24h")
        sent = interaction.followup.send.await_args.kwargs
        assert sent["ephemeral"] and "Pilot P&L" in sent["content"]
        assert sent["file"].filename == "oil-live-chart.png"
        assert sent["file"].fp.read(8) == b"\x89PNG\r\n\x1a\n"
        sent["file"].close()
    asyncio.run(run())
