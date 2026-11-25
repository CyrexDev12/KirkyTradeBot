import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from bot.api import PredictionAPI
from bot.discord_bot import NFLBot
from bot.oil_paper_test import CURSORS, ROUTE, OilPaperTestPublisher, event_card
from bot.paper import PaperPosts, PaperPublisher, receipt
from bot.storage import Store


def account(**changes):
    return {"available": True, "latest_id": 1, "entries_allowed": False, "state": "standby",
            "message": "live_entries_enabled", "last_heartbeat": "2026-11-25T12:00:00+00:00",
            "starting_budget": "100", "balance": "99.95", "realized_pnl": "-0.05", "buying_power": "99.95",
            "entry_cap": "10.00", "loss_cap": "20.00", **changes}


def test_paper_test_discord_views_and_channel_configuration(tmp_path):
    async def run():
        api = SimpleNamespace(oil_paper_test=AsyncMock(return_value=account()))
        bot = NFLBot(api, Store(tmp_path / "bot.db"), 12, 123)
        bot.paper_publisher.deliver = AsyncMock(return_value=1)
        interaction = SimpleNamespace(response=AsyncMock(), followup=AsyncMock(), guild_id=12,
                                      guild=SimpleNamespace(me=object()))
        channel = SimpleNamespace(id=55, mention="<#55>", guild=SimpleNamespace(id=12),
                                  permissions_for=lambda _: discord.Permissions.all())
        configure = bot.tree.get_command("oilpaperlivetestchannel")
        await configure.callback(interaction, channel)
        assert bot.paper_posts.channels(12) == {ROUTE: 55}
        first_card = bot.paper_publisher.deliver.await_args.args[1]["payload"]
        assert "PAPER ONLY" in first_card["footer"]
        assert dict(first_card["fields"])["Paper limits"] == "$10.00 per entry · $20.00 realized loss limit"
        group = bot.tree.get_command("oilpaperlivetest")
        await group.get_command("status").callback(interaction)
        status = interaction.followup.send.await_args.kwargs
        assert status["ephemeral"] and "$99.95" in str(status["embed"].to_dict())
        api.oil_paper_test.return_value = {
            "available": True, "starting_budget": "100", "balance": "99.95", "pilot_pnl": "-.05",
            "range_pnl": "-.05", "closed_in_range": 1, "open_trade": False,
            "points": [{"at": "2026-11-25T18:00:00+00:00", "balance": "100"},
                       {"at": "2026-11-25T19:00:00+00:00", "balance": "99.95"},
                       {"at": "2026-11-25T20:00:00+00:00", "balance": "99.95"}]}
        await group.get_command("graph").callback(interaction)
        api.oil_paper_test.assert_awaited_with("/chart", time="24h")
        graph = interaction.followup.send.await_args.kwargs
        assert "PAPER ONLY" in graph["content"] and "Account P&L -$0.05" in graph["content"] and graph["ephemeral"]
        assert graph["file"].filename == "oil-paper-test.png" and graph["file"].fp.read(4) == b"\x89PNG"
        graph["file"].close()
    asyncio.run(run())


def test_paper_test_recovers_receipts_and_cursor_on_channel_change_and_restart(tmp_path):
    async def run():
        db = tmp_path / "bot.db"
        current = account()
        events = [{"id": 1, "kind": "status"}]
        async def read(path, **params):
            if path == "/status":
                return current
            assert path == "/events" and params["limit"] == 25
            return {"events": [e for e in events if e["id"] > params["after"]]}
        api = SimpleNamespace(oil_paper_test=read)
        messages = {}
        uncertain = receipt(f"{ROUTE}:2")
        class Transport:
            async def send(self, channel, embed, nonce):
                marker = embed.footer.text.split(" · ")[-1]
                key = channel, marker
                assert key not in messages
                messages[key] = embed.to_dict()
                if marker == uncertain:
                    raise TimeoutError("Discord response lost after successful send")
                return len(messages)
            async def find_existing(self, channel, pid, started_at):
                return 123 if (channel, receipt(pid)) in messages else None

        def publisher():
            posts = PaperPosts(db)
            return OilPaperTestPublisher(api, posts, PaperPublisher(api, posts, Transport()), 12)

        runner = publisher()
        assert await runner.configure(60)
        assert runner.posts.desk_routes(ROUTE, CURSORS)[0]["event_id"] == 1
        events.extend([{"id": 2, "kind": "entry_partial", "side": "UP", "quantity": "1", "requested_quantity": "2",
                        "price": ".40", "limit_price": ".42", "balance": "100", "realized_pnl": "0"},
                       {"id": 3, "kind": "trade_closed", "net_pnl": "-.05", "balance": "99.95",
                        "realized_pnl": "-.05"}])
        with pytest.raises(TimeoutError):
            await runner.tick()
        runner = publisher()
        current["latest_id"] = 3
        assert await runner.configure(61)
        await runner.tick()
        await runner.tick()
        posted = [(c, e["title"]) for (c, _), e in messages.items()]
        assert posted == [(60, "Oil D paper test · Account status"), (60, "Oil D paper test · Entry partially filled"),
                          (61, "Oil D paper test · Account status"), (61, "Oil D paper test · Trade closed")]
        assert runner.posts.desk_routes(ROUTE, CURSORS)[0]["event_id"] == 3
        assert (61, receipt(f"{ROUTE}:1")) not in messages
        fields = dict((f["name"], f["value"]) for f in messages[(60, uncertain)]["fields"])
        assert fields["Fill price per contract"] == "40¢" and fields["Order limit"] == "42¢"
        assert fields["Realized paper balance"] == "$100.00" and fields["Account realized P&L"] == "$0.00"
        current.update(state="offline", health_current=False, entries_allowed=False)
        await runner.tick()
        await publisher().tick()
        current.update(state="standby", health_current=True)
        await publisher().tick()
        assert len(messages) == 6  # One outage and one recovery snapshot across restarts.
        assert all("PAPER ONLY" in item["footer"]["text"] for item in messages.values())
    asyncio.run(run())


def test_paper_test_client_only_reads_authenticated_paper_endpoint():
    async def run():
        request_log = []
        async def handle(request):
            request_log.append(request)
            return httpx.Response(200, json={"events": []})
        api = PredictionAPI("http://test", token="test-operator")
        await api.close()
        api.client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
        try:
            await api.oil_paper_test("/events", after=14)
        finally:
            await api.close()
        req, = request_log
        assert req.method == "GET" and req.url.path == "/api/wti/paper-execution/events"
        assert req.url.params["after"] == "14" and req.headers["X-Paper-Operator-Token"] == "test-operator"
    asyncio.run(run())


def test_paper_test_gate_and_exit_cards_keep_execution_semantics():
    gate = event_card({"kind": "entry_gate", "state": "standby", "entries_allowed": False,
                       "reason": "live_entries_enabled", "balance": "100", "realized_pnl": "0"})
    assert gate["description"] == "live entries enabled" and dict(gate["fields"])["New paper entries"] == "On hold"
    stop = event_card({"kind": "state_changed", "exit_kind": "stop_triggered", "limit_price": "0.21"})
    assert stop["title"].endswith("Stop triggered") and dict(stop["fields"])["Order limit"] == "21¢"
    close = event_card({"kind": "trade_closed", "net_pnl": "-0.16", "realized_pnl": "0.50", "balance": "100.50"})
    assert dict(close["fields"]) == {"Trade net P&L": "-$0.16", "Realized paper balance": "$100.50",
                                     "Account realized P&L": "$0.50"}
