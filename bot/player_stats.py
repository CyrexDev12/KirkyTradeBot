"""Discord player forecasts and weekly yardage boards."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
import json
import logging
from typing import Literal
from urllib.parse import urlparse

import discord
from discord import app_commands
import httpx

from .graphs import player_yards_graph, team_points_graph
from .messages import percent, timestamp
from .paper import utcnow

logger = logging.getLogger(__name__)
PICK_LIMIT = 10
YardageStat = Literal["rushing", "passing"]
GraphData = Literal["team-points", "rushing", "passing"]
Season = app_commands.Range[int, 1999, 2100]
PlayerSeason = app_commands.Range[int, 2015, 2100]
Week = app_commands.Range[int, 1, 22]


def _yards(value):
    return f"{value:.1f} yd" if value is not None else "Unavailable"


def _interval(projection):
    low, high = projection.get("interval_low"), projection.get("interval_high")
    return f"{low:.1f}–{high:.1f} yd" if low is not None and high is not None else "Insufficient history"


def projection_embed(forecast):
    embed = discord.Embed(
        title=f"{forecast['player']} · {forecast['stat'].title()} yards",
        description=(f"{forecast['position']} · {forecast['team']} vs {forecast['opponent']} · "
                     f"{forecast['season']} week {forecast['week']} · {timestamp(forecast['kickoff'])}"),
        colour=0x5EA5E0,
    )
    embed.add_field(name="Projection", value=_yards(forecast["projection"]))
    embed.add_field(name="80% prediction interval", value=_interval(forecast))
    embed.add_field(name="Season average", value=_yards(forecast.get("season_average")))
    if forecast.get("robust_season_average") is not None:
        embed.add_field(name="Outlier adjusted average", value=_yards(forecast["robust_season_average"]))
    embed.add_field(name="Opponent allows per game", value=_yards(forecast.get("opponent_allowed_average")))
    if forecast.get("expected_attempts") is not None:
        embed.add_field(name="Expected attempts", value=f"{forecast['expected_attempts']:.1f}")
    embed.add_field(name="Prior games in model sample", value=str(forecast["games_sampled"]))
    footer = "Assumes the player participates · Use /pygraph to inspect the data"
    if forecast.get("kind") == "historical_reconstruction":
        footer = f"Historical pregame reconstruction · {footer}"
    embed.set_footer(text=footer)
    return embed


def _pick_field(rank, pick):
    label = f"{rank}. {pick['player']} · {pick['stat'].title()}"
    edge = f"{100 * pick['edge']:+.1f} pp"
    terms = f"{pick['side'].title()} {pick['line']:g} yd"
    market_url = pick.get("market_url")
    if market_url:
        parsed = urlparse(market_url)
        if parsed.scheme == "https" and parsed.netloc in {"polymarket.us", "www.polymarket.us", "polymarket.com"}:
            terms = f"[{terms}](<{market_url}>)"
    venue_line = f" · Venue line {pick['market_line_label']}" if pick.get("market_line_label") else ""
    quote = f"{100 * pick['price']:.1f}¢"
    if pick.get("quantity") is not None:
        quote += f" for {pick['quantity']:g} contracts"
    if pick.get("quote_at"):
        quote += f" at {timestamp(pick['quote_at'], 't')}"
    text = (f"**{terms}** · {pick['team']} vs {pick['opponent']} · {timestamp(pick['kickoff'])}\n"
            f"Projection {_yards(pick['projection'])} · 80% interval {_interval(pick)}{venue_line}\n"
            f"Model {percent(pick['model_probability'])} · Quote {quote} · Net edge {edge}")
    return label, text


class PlayerPicksPublisher:
    """Keep one message per server and week. Add new picks until the board is full."""

    def __init__(self, api, posts, publisher, clock=utcnow):
        self.api, self.posts, self.publisher, self.clock = api, posts, publisher, clock
        with closing(posts.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS player_pick_boards (
                season INTEGER NOT NULL, week INTEGER NOT NULL, picks TEXT NOT NULL,
                PRIMARY KEY (season, week))""")

    def _board(self, season, week, candidates):
        now = self.clock()
        with closing(self.posts.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT picks FROM player_pick_boards WHERE season=? AND week=?",
                             (season, week)).fetchone()
            picks = json.loads(row["picks"]) if row else []
            included = {(p["game_id"], p["player_id"], p["stat"]) for p in picks}
            count = len(picks)
            for pick in candidates:
                if len(picks) == PICK_LIMIT:
                    break
                key = (pick["game_id"], pick["player_id"], pick["stat"])
                if key not in included and datetime.fromisoformat(pick["kickoff"]) > now:
                    picks.append(pick)
                    included.add(key)
            if len(picks) != count:
                db.execute("INSERT OR REPLACE INTO player_pick_boards VALUES (?, ?, ?)",
                           (season, week, json.dumps(picks)))
        return picks

    async def tick(self):
        routes = self.posts.routes("players")
        if not routes:
            return
        slate = await self.api.player_picks()
        season, week = slate.get("season"), slate.get("week")
        if season is None or week is None:
            return
        picks = self._board(season, week, slate.get("picks", []))
        if not picks:
            return
        publication_id = f"player-picks:{season}:{week}"
        payload = {"title": f"NFL player yardage picks · {season} week {week}",
                   "description": (f"{len(picks)} of {PICK_LIMIT} picks · Prices and estimates are snapshots from selection.\n"
                                   "Forecasts assume a qualifying snap. With zero qualifying snaps, the venue "
                                   "settles using its last fair market price."),
                   "fields": [_pick_field(i, pick) for i, pick in enumerate(picks, 1)],
                   "footer": "PAPER PICKS · 80% prediction intervals"}
        item = {"publication_id": publication_id, "revision": len(picks), "payload": payload}
        upcoming = any(datetime.fromisoformat(pick["kickoff"]) > self.clock() for pick in picks)
        for route in routes:
            if not upcoming and not self.posts.post(route["guild_id"], publication_id):
                continue
            try:
                await self.publisher.deliver(route, item)
            except Exception as exc:
                self.posts.failed(route["guild_id"], publication_id, type(exc).__name__)
                logger.warning("Player picks delivery failed (%s)", type(exc).__name__)


async def _lookup(interaction, request):
    try:
        return await request
    except httpx.HTTPStatusError as exc:
        messages = {
            404: "No data matched that player or team and week. Check the spelling and selected season.",
            409: "More than one player matched. Use the full name and team abbreviation.",
            422: "Check the player name or team abbreviation, season, and week.",
        }
        message = messages.get(exc.response.status_code)
        if message is None:
            raise
        try:
            payload = exc.response.json()
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, str) and detail:
                message = detail[:250]
        except ValueError:
            pass
        await interaction.followup.send(message, ephemeral=True)
        return None


def register_player_commands(bot):
    @bot.tree.command(name="player-stats", description="Get a pregame rushing or passing yardage forecast")
    @app_commands.describe(player="Player's full name", stat="Rushing or passing yards",
                           team="Optional player team abbreviation")
    async def player_stats(interaction: discord.Interaction, player: app_commands.Range[str, 2, 100],
                           stat: YardageStat, team: app_commands.Range[str, 2, 4] | None = None,
                           season: PlayerSeason | None = None, week: Week | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        forecast = await _lookup(interaction, bot.api.player_projection(player, stat, team=team, season=season, week=week))
        if forecast:
            await interaction.followup.send(embed=projection_embed(forecast), ephemeral=True)

    @bot.tree.command(name="pygraph", description="Inspect team scoring or player yardage as a graph and CSV")
    @app_commands.describe(dataset="Team points, rushing yards, or passing yards",
                           subject="NFL team abbreviation for team points, full player name for yardage",
                           week="Forecast week for player yardage", team="Optional player team abbreviation")
    async def pygraph(interaction: discord.Interaction, dataset: GraphData,
                      subject: app_commands.Range[str, 2, 100], season: Season | None = None,
                      week: Week | None = None, team: app_commands.Range[str, 2, 4] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if dataset == "team-points":
            data = await _lookup(interaction, bot.api.team_points(subject, season=season))
            render = team_points_graph
        else:
            data = await _lookup(interaction, bot.api.player_projection(subject, dataset, team=team,
                                                                       season=season, week=week))
            render = player_yards_graph
        if data is None:
            return
        try:
            png, csv_data = await asyncio.to_thread(render, data)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        note = data.get("method") if dataset == "team-points" else None
        if data.get("kind") == "historical_reconstruction":
            note = "Historical pregame reconstruction."
        await interaction.followup.send(content=note, files=[discord.File(png, filename="pygraph.png"),
                                               discord.File(csv_data, filename="pygraph.csv")], ephemeral=True)
