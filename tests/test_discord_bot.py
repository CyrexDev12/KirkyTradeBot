import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot.discord_bot import MatchupView, NFLBot
from bot.messages import matchup_embed, week_embed
from bot.paper import receipt
from bot.scheduler import AlertScheduler, due
from bot.storage import Store


@pytest.fixture
def payload():
    game = dict(game_id="2026_11_IND_HOU", season=2026, week=11, home="HOU", away="IND",
                home_team={"name":"Houston Texans"}, away_team={"name":"Indianapolis Colts"},
                kickoff="2026-11-20T01:15:00+00:00", kickoff_confirmed=True,
                home_score=None, away_score=None, status="Upcoming")
    return dict(game=game, schedule_stale=False,
                prediction=dict(home_probability=.6,kind="live_pregame",generated_at="2026-11-19T23:00:00+00:00"),
                total_prediction=dict(predicted_total=44.5, interval_low=30,interval_high=60,
                                      kind="live_pregame",generated_at="2026-11-19T23:00:00+00:00"),
                monte_carlo=dict(status="available",probability_home_win=.55,probability_away_win=.42,
                                 probability_tie=.03,home_win_given_no_tie=.55/.97,
                                 distributions=dict(home=dict(mean=25),away=dict(mean=22),
                                                    total=dict(mean=47,interval_80=[28,65])),
                                 simulation_count=50000,residual_pair_count=100,as_of="2026-11-19T23:00:00+00:00"))


class API:
    def __init__(self, payload):
        self.payload = payload
        self.feed = {"games":[payload["game"]],"schedule_stale":False}

    async def upcoming(self):
        return deepcopy(self.feed)

    async def forecast(self, game_id):
        return deepcopy(self.payload)

    async def close(self):
        pass


class Transport:
    def __init__(self):
        self.messages = {}
        self.fail_after_send = False
        self.fail_history = False

    async def find_existing(self, row):
        if self.fail_history:
            raise PermissionError()
        return self.messages.get((row["guild_id"],row["game_id"]))

    async def send(self, channel_id, row, embed):
        key = (row["guild_id"],row["game_id"])
        assert key not in self.messages
        self.messages[key] = 1234
        if self.fail_after_send:
            raise TimeoutError()
        return 1234


def scheduler(payload, tmp_path):
    api, transport = API(payload), Transport()
    store = Store(tmp_path/"discord.db")
    store.configure(12,34,30)
    clock = [datetime.fromisoformat(payload["game"]["kickoff"])-timedelta(minutes=30)]
    runner = AlertScheduler(api,store,transport,clock=lambda:clock[0])
    return runner, clock


def test_alert_window_and_invalid_games(payload):
    game = payload["game"]
    kickoff = datetime.fromisoformat(game["kickoff"])
    assert due(game,kickoff-timedelta(minutes=30),30)
    assert not due(game,kickoff-timedelta(minutes=31),30)
    assert not due(game,kickoff,30)
    assert not due(game,kickoff+timedelta(minutes=1),30)
    for change in (dict(kickoff_confirmed=False),dict(home_score=24),dict(source_status="postponed")):
        assert not due({**game,**change},kickoff-timedelta(minutes=10),30)


def test_delivery_survives_restart_and_preserves_posted_predictions(payload,tmp_path):
    runner, clock = scheduler(payload,tmp_path)
    asyncio.run(runner.tick())
    row = runner.store.last_delivery(12)
    assert row["message_id"] == 1234
    assert '"home_probability": 0.6' in row["payload"]
    runner.api.payload["prediction"]["home_probability"] = .9
    restart = AlertScheduler(runner.api,Store(runner.store.path),runner.transport,clock=lambda:clock[0])
    asyncio.run(restart.tick())
    assert restart.store.last_delivery(12)["payload"] == row["payload"]
    assert len(runner.transport.messages) == 1


def test_uncertain_send_is_reconciled_with_channel_history(payload,tmp_path):
    runner, clock = scheduler(payload,tmp_path)
    runner.transport.fail_after_send = True
    asyncio.run(runner.tick())
    assert not runner.store.delivered(12,payload["game"]["game_id"])
    clock[0] += timedelta(minutes=1)
    runner.transport.fail_after_send = False
    asyncio.run(runner.tick())
    assert runner.store.delivered(12,payload["game"]["game_id"])
    assert len(runner.transport.messages) == 1


def test_failed_history_lookup_preserves_uncertain_delivery(payload,tmp_path):
    runner, clock = scheduler(payload,tmp_path)
    config = runner.store.settings(12)
    row = runner.store.claim(config,payload["game"],payload,clock[0])
    assert runner.store.claim(config,payload["game"],payload,clock[0]) is None
    clock[0] += timedelta(minutes=6)
    runner.transport.fail_history = True
    asyncio.run(runner.tick())
    assert runner.transport.messages == {}
    assert runner.store.last_delivery(12)["error"] == "PermissionError"


def test_new_channel_reconciles_old_channel_before_any_new_send(payload,tmp_path):
    runner, clock = scheduler(payload,tmp_path)
    runner.transport.fail_after_send = True
    asyncio.run(runner.tick())
    runner.store.configure(12,99,30)
    asyncio.run(runner.tick())
    assert runner.store.last_delivery(12)["message_id"] == 1234
    assert runner.store.last_delivery(12)["channel_id"] == 34


def test_schedule_changes_disable_and_cold_start(payload,tmp_path):
    runner, clock = scheduler(payload,tmp_path)
    kickoff = datetime.fromisoformat(payload["game"]["kickoff"])
    runner.api.payload["game"]["kickoff"] = (kickoff+timedelta(hours=2)).isoformat()
    asyncio.run(runner.tick())
    assert runner.transport.messages == {}
    runner.api.payload["game"]["kickoff"] = kickoff.isoformat()
    runner.api.payload["prediction"] = None
    asyncio.run(runner.tick())
    assert runner.transport.messages == {}
    clock[0] = kickoff-timedelta(minutes=1)
    runner.store.disable(12)
    asyncio.run(runner.tick())
    assert runner.transport.messages == {}
    runner.store.configure(12,34,30)
    asyncio.run(runner.tick())
    assert len(runner.transport.messages) == 1


def test_stale_schedule_and_kickoff_crossing_never_send(payload,tmp_path):
    runner, clock = scheduler(payload,tmp_path)
    runner.api.feed["schedule_stale"] = True
    asyncio.run(runner.tick())
    assert runner.transport.messages == {}
    runner.api.feed["schedule_stale"] = False
    original = runner.api.forecast
    async def slow(game_id):
        clock[0] = datetime.fromisoformat(payload["game"]["kickoff"])
        return await original(game_id)
    runner.api.forecast = slow
    asyncio.run(runner.tick())
    assert runner.transport.messages == {}


def test_simultaneous_games_and_separate_servers(payload,tmp_path):
    runner, clock = scheduler(payload,tmp_path)
    runner.store.configure(56,78,30)
    second = {**payload["game"],"game_id":"2026_11_AZ_KC"}
    runner.api.feed["games"].append(second)
    async def forecast(game_id):
        return {**payload,"game":{**payload["game"],"game_id":game_id}}
    runner.api.forecast = forecast
    asyncio.run(runner.tick())
    assert len(runner.transport.messages) == 4


def test_buttons_embeds_and_runtime_admin_permissions(payload,tmp_path):
    async def check():
        bot = NFLBot(API(payload),Store(tmp_path/"bot.db"),12,123)
        commands = {c.name:c for c in bot.tree.get_commands()}
        assert set(commands) == {"weeklymatchup","matchup","nextgame","botstatus","help","alerts",
                                 "picks","trade","account","performance","why-no-trades","health",
                                 "paper-pause","paper-resume","paper-channels","parlay","crypto-account","crypto-trades","crypto-stats"}
        assert bot.intents.value == 1
        for command in commands.values():
            command.to_dict(bot.tree)
        setup = commands["alerts"].get_command("setup")
        with pytest.raises(discord.app_commands.MissingPermissions):
            setup.checks[0](SimpleNamespace(permissions=discord.Permissions.none()))
        assert setup.checks[0](SimpleNamespace(permissions=discord.Permissions(manage_guild=True)))
        channel_setup = commands["paper-channels"].get_command("set")
        with pytest.raises(discord.app_commands.MissingPermissions):
            channel_setup.checks[0](SimpleNamespace(permissions=discord.Permissions.none()))
        assert {c.value for c in channel_setup.parameters[0].choices} == {"picks", "updates", "reports", "parlay", "crypto"}
        games = [{**payload["game"],"game_id":str(i)} for i in range(16)]
        view = MatchupView(games)
        assert len(view.children) == 16
        assert len(view.to_components()) == 4
        assert [b.game_id for b in view.children] == [str(i) for i in range(16)]
        view.stop()
        assert len(week_embed({"season":2026,"week":11,"games":games})) < 6000
    asyncio.run(check())
    embed = matchup_embed(payload)
    assert len(embed) < 6000
    text = str(embed.to_dict())
    assert "tie 3.0%" in text and "ties" in text
    assert "Gradient model" in text and "Monte Carlo" in text
    payload["monte_carlo"] = {"status":"unavailable","unavailable_reason":"Insufficient current-season data"}
    payload["prediction"]["kind"] = "walk_forward_reconstruction"
    text = str(matchup_embed(payload).to_dict())
    assert "Insufficient current-season data" in text and "Historical reconstruction" in text


def test_crypto_reconciles_before_cursor_advances_and_preserves_cursor_on_reroute(tmp_path):
    from bot.crypto import CryptoPublisher
    from bot.paper import PaperPosts, PaperPublisher
    posts = PaperPosts(tmp_path / "bot.sqlite")
    posts.configure_crypto(1, 11, 10)
    slot = "2026-11-20T13:30:00+00:00"
    trade = {"slot_at": slot, "status": "OPEN", "direction": "UP", "quantity": 0.001,
             "entry_price": 20000, "stop_loss": 19900, "take_profit": 20300}
    events = [{"id": 11, "kind": "opened", "trade": trade},
              {"id": 12, "kind": "closed", "trade": {**trade, "status": "CLOSED", "exit_price": 20301,
                                                        "exit_reason": "target", "net_pnl": 0.25}}]
    sent = {}
    cursors = []

    class EventAPI:
        async def crypto(self, path, after, limit):
            assert path == "/events"
            cursors.append(after)
            return {"events": [e for e in events if e["id"] > after], "latest_id": 12}

    class Discord:
        async def find_existing(self, channel, publication_id, started_at):
            return sent.get((channel, receipt(publication_id)))

        async def send(self, channel, embed, nonce):
            assert int(channel) in (11, 22)
            publication_id = embed.footer.text.split(" · ")[-1]
            assert (channel, publication_id) not in sent
            sent[channel, publication_id] = len(sent) + 1
            if len(sent) == 1:
                raise TimeoutError("already delivered")
            return sent[channel, publication_id]

    def poll():
        reopened = PaperPosts(posts.path)
        runner = CryptoPublisher(EventAPI(), reopened, PaperPublisher(None, reopened, Discord()))
        asyncio.run(runner.tick())

    poll()
    assert posts.crypto_routes()[0]["event_id"] == 10
    posts.configure_crypto(1, 22, 12)
    poll()
    poll()
    assert cursors == [10, 10, 12]
    assert set(sent) == {(11, receipt(f"crypto:{slot}:opened")), (22, receipt(f"crypto:{slot}:closed"))}
    assert posts.crypto_routes()[0]["event_id"] == 12


def test_crypto_cards_distinguish_planned_loss_and_profit_from_realized_results():
    from bot.crypto import crypto_account_card, crypto_trade_card
    trade = {"slot_at": "2026-11-20T13:30:00+00:00", "status": "OPEN", "direction": "DOWN", "quantity": .0001,
             "entry_price": 80000, "stop_loss": 80500, "take_profit": 78900, "risk_at_stop": .08}
    legacy = dict(crypto_trade_card(trade)["fields"])
    assert legacy["Estimated outcomes after costs"] == "Loss at stop $0.08"
    current = {**trade, "target_net_profit": .12}
    expected = "Loss at stop $0.08 · Profit at target $0.12"
    assert dict(crypto_trade_card(current)["fields"])["Estimated outcomes after costs"] == expected
    account = {"balance": 100, "starting_balance": 100, "available_cash": 92, "realized_pnl": 0,
               "closed_trades": 0, "open_trade": current}
    assert dict(crypto_account_card(account)["fields"])["Estimated outcomes after costs"] == expected
    closed = {**current, "status": "CLOSED", "exit_price": 79500, "exit_reason": "window_end", "net_pnl": .03}
    close = crypto_trade_card(closed)
    assert "$0.03 after costs" in close["title"] and "15 minutes up" in close["description"]
    assert "Estimated outcomes after costs" not in dict(close["fields"])
    closed["record"] = {"closed_trades": 4, "wins": 3, "losses": 1, "flats": 0, "win_rate": .75}
    assert "75%" in dict(crypto_trade_card(closed)["fields"])["📒 Running record"]


def test_parlay_and_crypto_commands_use_isolated_feeds(payload, tmp_path):
    async def check():
        guild_id = 12
        api = API(payload)
        bot = NFLBot(api, Store(tmp_path / "bot.db"), guild_id, 123)
        interaction = SimpleNamespace(response=AsyncMock(), followup=AsyncMock(), guild_id=guild_id,
                                      guild=SimpleNamespace(me=object()))
        channel = SimpleNamespace(guild=SimpleNamespace(id=guild_id), id=88, mention="<#88>",
                                  permissions_for=lambda member: discord.Permissions.all())
        cmd = bot.tree.get_command("paper-channels").get_command("set")
        parlay_kind = discord.app_commands.Choice(name="parlay", value="parlay")
        await cmd.callback(interaction, parlay_kind, channel)
        assert bot.paper_posts.channels(guild_id) == {"parlay": 88}

        api.paper = AsyncMock(return_value={"card": {"title": "Week 11 parlay", "footer": "INFORMATIONAL"}})
        await bot.tree.get_command("parlay").callback(interaction)
        api.paper.assert_awaited_once_with("/parlay")
        assert interaction.followup.send.await_args.kwargs["embed"].title == "Week 11 parlay"
        crypto_kind = discord.app_commands.Choice(name="crypto", value="crypto")
        api.crypto = AsyncMock(return_value={"latest_id": 10})
        await cmd.callback(interaction, crypto_kind, channel)
        assert bot.paper_posts.configured_nfl_routes() == ["parlay"]
        assert bot.paper_posts.crypto_routes() == [{"guild_id": guild_id, "channel_id": 88, "event_id": 10}]

        api.crypto.return_value = {"balance": 99.92, "starting_balance": 100, "available_cash": 99.92,
                                   "realized_pnl": -0.08, "closed_trades": 1, "open_trade": None}
        await bot.tree.get_command("crypto-account").callback(interaction)
        api.crypto.assert_awaited_with("/account")
        result = interaction.followup.send.await_args.kwargs["embed"]
        assert "BTC paper account" in result.title and "$99.92" in result.description
        api.crypto.return_value = {"trades": [{"slot_at": "2026-11-20T13:30:00+00:00", "status": "SKIPPED",
                                               "reason": "conflicting trend"}]}
        await bot.tree.get_command("crypto-trades").callback(interaction)
        api.crypto.assert_awaited_with("/trades", limit=10)
        assert "conflicting trend" in interaction.followup.send.await_args.kwargs["embed"].description
        api.crypto.return_value = {"closed_trades": 0, "wins": 0, "losses": 0, "flats": 0,
                                   "win_rate": None, "skipped": 3, "net_pnl": 0, "patterns": [],
                                   "learning": {"min_trades": 20}}
        await bot.tree.get_command("crypto-stats").callback(interaction)
        api.crypto.assert_awaited_with("/stats")
        stats = interaction.followup.send.await_args.kwargs["embed"]
        assert "3 skipped" in stats.description and "first paper trade" in stats.description
    asyncio.run(check())


def test_btc_scorecard_separates_all_time_record_and_recent_pattern():
    from bot.crypto import crypto_stats_card
    stats = {"closed_trades": 50, "wins": 20, "losses": 29, "flats": 1,
             "net_pnl": -1.43, "win_rate": .4, "skipped": 100,
             "patterns": [{"label": "Up · 6 AM Arizona", "wins": 6, "closed_trades": 20,
                           "net_pnl": -1.21, "risk_multiplier": .5}],
             "learning": {"min_trades": 20, "lookback_days": 90, "lookback": 40}}
    card = crypto_stats_card(stats)
    assert "40%" in card["description"] and "20W / 29L / 1 flat" in card["description"]
    assert "-$1.43" in card["description"] and "100 skipped" in card["description"]
    name, value = card["fields"][0]
    assert "90 days" in name and "6/20 won" in value and "risk cut to 50%" in value


def test_sports_only_api_does_not_fetch_markets(payload,monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app
    monkeypatch.setattr(app,"_all_games",lambda:[payload["game"]])
    monkeypatch.setattr(app,"game_forecast",lambda game:payload)
    monkeypatch.setattr(app,"upcoming",lambda hours:{"games":[payload["game"]],"schedule_stale":False})
    async def forbidden(*args,**kwargs):
        raise AssertionError("Sports feed requested market data")
    monkeypatch.setattr(app,"get_week_markets",forbidden)
    client = TestClient(app.app)
    assert client.get("/healthz").json() == {"status":"ok"}
    assert client.get("/api/upcoming").json()["games"][0]["game_id"] == payload["game"]["game_id"]
    result = client.get("/api/games/2026_11_IND_HOU/forecast").json()
    assert result["prediction"]["home_probability"] == .6
    assert result["monte_carlo"]["probability_tie"] == .03
    assert client.get("/api/games/missing/forecast").status_code == 404


def test_channel_setup_checks_both_destinations_and_keeps_cursor(payload, tmp_path):
    async def check():
        api = API(payload)
        api.crypto = AsyncMock(return_value={"latest_id": 15})
        bot = NFLBot(api, Store(tmp_path / "bot.db"), 12, 123)
        group = bot.tree.get_command("paper-channels")
        setup = group.get_command("setup")
        with pytest.raises(discord.app_commands.MissingPermissions):
            setup.checks[0](SimpleNamespace(permissions=discord.Permissions.none()))
        interaction = SimpleNamespace(response=AsyncMock(), followup=AsyncMock(), guild_id=12,
                                      guild=SimpleNamespace(me=object()))
        def channel(number, allowed=True):
            return SimpleNamespace(id=number, mention=f"<#{number}>", guild=SimpleNamespace(id=12),
                                   permissions_for=lambda _: discord.Permissions.all() if allowed else discord.Permissions.none())
        await setup.callback(interaction, channel(31), channel(32, False))
        assert bot.paper_posts.channels(12) == {}
        api.crypto.assert_not_called()
        await setup.callback(interaction, channel(31), channel(32))
        api.crypto.return_value = {"latest_id": 18}
        await setup.callback(interaction, channel(41), channel(42))
        assert bot.paper_posts.crypto_routes() == [{"guild_id": 12, "channel_id": 41, "event_id": 15}]
        await group.get_command("show").callback(interaction)
        output = interaction.response.send_message.await_args.args[0]
        assert "<#41>" in output and "<#42>" in output
    asyncio.run(check())


def test_current_parlay_follows_configured_channel_without_duplicate_posts(tmp_path):
    from backend.trading.store import Store as NFLStore
    from bot.paper import PaperPosts, PaperPublisher

    async def publish():
        store = NFLStore(tmp_path / "nfl.sqlite")
        posts = PaperPosts(tmp_path / "bot.sqlite")
        now = datetime.now(timezone.utc)
        item = {"publication_id": "parlay:2026:11", "kind": "weekly_parlay", "channel": "parlay",
                "revision": 1, "payload": {"title": "Weekly board", "expires_at": (now + timedelta(hours=2)).isoformat()}}
        store.enqueue([item], now)
        class API:
            async def paper_outbox(self, channels):
                return {"publications": store.pending_publications(channels=channels, include_current_parlay=True)}
            async def paper_ack(self, pid, rev, msg, err):
                store.ack_publication(pid, rev, msg, err, now)
        transport = SimpleNamespace(send=AsyncMock(side_effect=[101, 102, 103]), edit=AsyncMock())
        publisher = PaperPublisher(API(), posts, transport)
        posts.set_channel(1, "parlay", 50)
        await publisher.tick()
        posts.set_channel(2, "parlay", 60)
        await publisher.tick()
        posts.set_channel(1, "parlay", 70)
        await publisher.tick()
        await publisher.tick()
        assert [call.args[0] for call in transport.send.await_args_list] == [50, 60, 70]
        store.enqueue([{**item, "revision": 2}], now)
        await publisher.tick()
        assert {(c.args[0], c.args[1]) for c in transport.edit.await_args_list} == {(60, 102), (70, 103)}
        assert not store.pending_publications()
    asyncio.run(publish())
