import asyncio
import csv
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.discord_bot import NFLBot
from bot.paper import PaperPosts, PaperPublisher
from bot.player_stats import PlayerPicksPublisher
from bot.storage import Store


def forecast():
    return {
        "player": "Test Player", "player_id": "test-0", "position": "RB", "stat": "rushing",
        "team": "KC", "opponent": "DEN", "game_id": "2026_12_DEN_KC",
        "season": 2026, "week": 12, "kickoff": "2026-11-29T21:00:00+00:00",
        "projection": 73.5, "interval_low": 44, "interval_high": 110,
        "season_average": 77, "opponent_allowed_average": 95, "games_sampled": 8,
        "history": [{"season": 2026, "week": 11, "kickoff": "2026-11-22T21:00:00+00:00",
                     "opponent": "LV", "yards": 174, "robust_yards": 108, "outlier": True}],
    }


def test_player_stats_and_pygraph_return_forecast_and_inspection_files(tmp_path):
    async def check():
        api = SimpleNamespace(player_projection=AsyncMock(return_value=forecast()),
                              team_points=AsyncMock(return_value={"team": "KC", "season": 2026, "games": [
                                  {"week": 1, "opponent": "DEN", "points_for": 29, "points_against": 21,
                                   "robust_points_for": 29, "robust_points_against": 21}]}))
        bot = NFLBot(api, Store(tmp_path / "state.sqlite"))
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await bot.tree.get_command("player-stats").callback(interaction, "Test Player", "rushing", week=12)
        embed = interaction.followup.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Projection"] == "73.5 yd"
        assert fields["80% prediction interval"] == "44.0–110.0 yd"
        await bot.tree.get_command("pygraph").callback(interaction, "rushing", "Test Player", week=12)
        png, data = interaction.followup.send.await_args.kwargs["files"]
        assert png.fp.read(8) == b"\x89PNG\r\n\x1a\n"
        history, estimate = list(csv.DictReader(StringIO(data.fp.read().decode())))
        assert history["yards"] == "174" and history["robust_yards"] == "108"
        assert estimate["projection"] == "73.5" and estimate["interval_low"] == "44"
        await bot.tree.get_command("pygraph").callback(interaction, "team-points", "KC", season=2026)
        png, data = interaction.followup.send.await_args.kwargs["files"]
        assert png.fp.read(8) == b"\x89PNG\r\n\x1a\n"
        game, = csv.DictReader(StringIO(data.fp.read().decode()))
        assert game["points_for"] == game["robust_points_for"] == "29"
    asyncio.run(check())


def test_weekly_player_board_retries_and_appends_without_duplicate_posts(tmp_path):
    async def check():
        def pick(i, line=67.5):
            return {**forecast(), "player_id": f"test-{i}", "player": f"Player {i}", "line": line,
                    "price": 0.54, "model_probability": 0.65, "edge": 0.11, "side": "over"}

        def slate(picks, week=12):
            return {"season": 2026, "week": week, "picks": picks}

        clock = lambda: datetime(2026, 11, 25, tzinfo=timezone.utc)
        path = tmp_path / "state.sqlite"
        posts = PaperPosts(path)
        api = SimpleNamespace(player_picks=AsyncMock(side_effect=[slate([]), slate([pick(0)]),
            slate([pick(0, 75.5), *(pick(i) for i in range(1, 12))]), slate([pick(5)]), slate([pick(6)], week=13)]))
        transport = SimpleNamespace(send=AsyncMock(return_value=987), edit=AsyncMock())
        paper = PaperPublisher(api, posts, transport, clock)
        publisher = PlayerPicksPublisher(api, posts, paper, clock)
        await publisher.tick()
        api.player_picks.assert_not_awaited()
        posts.set_channel(123, "players", 45)
        assert posts.configured_nfl_routes() == []
        await publisher.tick()
        transport.send.assert_not_awaited()
        await publisher.tick()
        restarted_posts = PaperPosts(path)
        restarted_paper = PaperPublisher(api, restarted_posts, transport, clock)
        restarted = PlayerPicksPublisher(api, restarted_posts, restarted_paper, clock)
        await restarted.tick()
        assert transport.send.await_count == 1
        channel, message_id, embed = transport.edit.await_args.args
        assert (channel, message_id, len(embed.fields)) == (45, 987, 10)
        assert "67.5 yd" in embed.fields[0].value
        assert "75.5 yd" not in embed.fields[0].value
        await restarted.tick()
        assert transport.edit.await_count == 1
        restarted_posts.set_channel(123, "players", 46)
        await restarted.tick()
        assert transport.send.await_count == 2
        assert transport.send.await_args.args[0] == 46
    asyncio.run(check())
