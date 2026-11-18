import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from bot.discord_bot import MatchupView, NFLBot
from bot.messages import matchup_embed, week_embed
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
        assert set(commands) == {"weeklymatchup","matchup","nextgame","botstatus","help","alerts"}
        assert bot.intents.value == 1
        for command in commands.values():
            command.to_dict(bot.tree)
        setup = commands["alerts"].get_command("setup")
        with pytest.raises(discord.app_commands.MissingPermissions):
            setup.checks[0](SimpleNamespace(permissions=discord.Permissions.none()))
        assert setup.checks[0](SimpleNamespace(permissions=discord.Permissions(manage_guild=True)))
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
    assert "Final tie" in text and "3.0%" in text
    assert "Existing model" in text and "Monte Carlo" in text
    payload["monte_carlo"] = {"status":"unavailable","unavailable_reason":"Insufficient current-season data"}
    payload["prediction"]["kind"] = "walk_forward_reconstruction"
    text = str(matchup_embed(payload).to_dict())
    assert "Insufficient current-season data" in text and "Historical reconstruction" in text


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
