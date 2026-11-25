"""Run with python -m bot.discord_bot."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from .api import PredictionAPI
from .crypto import CryptoPublisher, register_crypto_commands
from .updown import GOLD, UpdownPublisher, register_gold_commands, register_updown_commands
from .messages import matchup_embed, timestamp, week_embed
from .paper import PaperPosts, PaperPublisher, PaperTransport, register_paper_commands
from .player_stats import PlayerPicksPublisher, register_player_commands
from .tracker import ChiefPublisher, register_chief_commands
from .oil import GAS, EnergyPublisher, OilPublisher, register_gas_commands, register_oil_commands
from .oil_live import OilLivePermissionRequired, OilLivePublisher, register_oil_live_commands
from .oil_paper_test import OilPaperTestPublisher, register_oil_paper_test_commands
from .scheduler import AlertScheduler, utcnow
from .storage import Store

logger = logging.getLogger(__name__)
ALERT_PERMISSIONS = ("view_channel", "send_messages", "embed_links", "read_message_history")


class DiscordTransport:
    def __init__(self, client):
        self.client = client

    async def channel(self, channel_id):
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Alerts require a server text channel")
        return channel

    async def find_existing(self, row):
        channel = await self.channel(row["channel_id"])
        after = datetime.fromisoformat(row["started_at"]) - timedelta(seconds=5)
        marker = f"NFL Fair Value · {row['game_id']} ·"
        async for message in channel.history(limit=None, after=after):
            if (message.author.id == self.client.user.id and message.content == "Pregame predictions"
                    and any((embed.footer.text or "").startswith(marker) for embed in message.embeds)):
                return message.id
        return None

    async def send(self, channel_id, row, embed):
        channel = await self.channel(channel_id)
        if utcnow() >= datetime.fromisoformat(row["kickoff"]):
            raise TimeoutError("Kickoff passed before the channel was ready")
        nonce = hashlib.sha256(f"{row['guild_id']}:{row['game_id']}".encode()).hexdigest()[:24]
        message = await channel.send("Pregame predictions", embed=embed, nonce=nonce,
                                     allowed_mentions=discord.AllowedMentions.none())
        return message.id


class MatchupButton(discord.ui.Button):
    def __init__(self, game, row):
        super().__init__(label=f"{game['away']} at {game['home']}", row=row, style=discord.ButtonStyle.secondary)
        self.game_id = game["game_id"]

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.client.show_game(interaction, self.game_id)


class MatchupView(discord.ui.View):
    def __init__(self, games):
        super().__init__(timeout=900)
        for i, game in enumerate(games):
            self.add_item(MatchupButton(game, i // 5))

    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)


async def report_error(interaction, error):
    logger.warning("Discord interaction failed (%s)", type(error).__name__)
    text = "Predictions are temporarily unavailable. Try again shortly or check /botstatus."
    if isinstance(error, OilLivePermissionRequired):
        text = str(error)
    elif isinstance(error, app_commands.CheckFailure):
        text = "This command requires the Manage Server permission."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


class NFLBot(discord.Client):
    def __init__(self, api, store, guild_id=None, application_id=None):
        super().__init__(intents=discord.Intents(guilds=True),
                         allowed_mentions=discord.AllowedMentions.none(), application_id=application_id)
        self.api, self.store, self.guild_id = api, store, guild_id
        self.tree = app_commands.CommandTree(self)
        self.scheduler = AlertScheduler(api, store, DiscordTransport(self))
        self.paper_posts = PaperPosts(store.path)
        self.paper_publisher = PaperPublisher(api, self.paper_posts, PaperTransport(self))
        self.crypto_publisher = CryptoPublisher(api, self.paper_posts, self.paper_publisher)
        self.updown_publisher = UpdownPublisher(api, self.paper_posts, self.paper_publisher)
        self.gold_publisher = UpdownPublisher(api, self.paper_posts, self.paper_publisher, GOLD)
        self.player_publisher = PlayerPicksPublisher(api, self.paper_posts, self.paper_publisher)
        self.chief_publisher = ChiefPublisher(api, self.paper_posts, self.paper_publisher)
        self.oil_publisher = OilPublisher(api, self.paper_posts, self.paper_publisher)
        self.oil_live_publisher = OilLivePublisher(api, self.paper_posts, self.paper_publisher, guild_id)
        self.oil_paper_test_publisher = OilPaperTestPublisher(api, self.paper_posts, self.paper_publisher, guild_id)
        self.gas_publisher = EnergyPublisher(api, self.paper_posts, self.paper_publisher, GAS)
        self.tree.on_error = report_error
        self.register_commands()
        register_paper_commands(self, self.paper_posts)
        register_crypto_commands(self)
        register_updown_commands(self)
        register_gold_commands(self)
        register_player_commands(self)
        register_chief_commands(self)
        register_oil_commands(self)
        register_oil_live_commands(self)
        register_oil_paper_test_commands(self)
        register_gas_commands(self)

    async def setup_hook(self):
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.post_alerts.start()
        self.post_crypto.start()
        self.post_oil_live.start()
        self.post_oil_paper_test.start()
        self.post_player_picks.start()

    async def close(self):
        self.post_alerts.cancel()
        self.post_crypto.cancel()
        self.post_oil_live.cancel()
        self.post_oil_paper_test.cancel()
        self.post_player_picks.cancel()
        await self.api.close()
        await super().close()

    @tasks.loop(seconds=60)
    async def post_alerts(self):
        if not self.is_ready():
            return
        try:
            await self.scheduler.tick()
        except Exception as exc:
            self.scheduler.last_error = f"Backend unavailable ({type(exc).__name__}). Retrying next minute."
            logger.warning("Alert poll failed (%s)", type(exc).__name__)
        if getattr(self.api, "token", None):
            try:
                await self.paper_publisher.tick()
            except Exception as exc:
                self.paper_publisher.last_error = f"Paper outbox unavailable ({type(exc).__name__})"
                logger.warning("Paper publication poll failed (%s)", type(exc).__name__)
        self.store.path.with_suffix(".heartbeat").write_text(str(time.time()))

    @post_alerts.before_loop
    async def before_alerts(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def post_crypto(self):
        if self.is_ready():
            await self.crypto_publisher.tick()
            await self.updown_publisher.tick()
            await self.gold_publisher.tick()
            await self.oil_publisher.tick()
            await self.gas_publisher.tick()
            await self.chief_publisher.tick()

    @tasks.loop(seconds=10)
    async def post_oil_live(self):
        if self.is_ready():
            try:
                await self.oil_live_publisher.tick()
            except Exception as exc:
                logger.warning("Oil D live update poll failed (%s)", type(exc).__name__)

    @tasks.loop(seconds=15)
    async def post_oil_paper_test(self):
        if self.is_ready():
            try:
                await self.oil_paper_test_publisher.tick()
            except Exception as exc:
                logger.warning("Oil D paper test update poll failed (%s)", type(exc).__name__)

    @tasks.loop(minutes=15)
    async def post_player_picks(self):
        if not self.is_ready():
            return
        try:
            await self.player_publisher.tick()
        except Exception as exc:
            logger.warning("Player picks poll failed (%s)", type(exc).__name__)

    @post_player_picks.before_loop
    async def before_player_picks(self):
        await self.wait_until_ready()

    async def show_game(self, interaction, game_id):
        payload = await self.api.forecast(game_id)
        await interaction.followup.send(embed=matchup_embed(payload), ephemeral=True)

    def register_commands(self):
        @self.tree.command(name="weeklymatchup", description="Choose any game this week and see both model predictions")
        @app_commands.guild_only()
        async def weeklymatchup(interaction: discord.Interaction, season: app_commands.Range[int, 1999, 2100] | None = None,
                                week: app_commands.Range[int, 1, 22] | None = None):
            await interaction.response.defer(ephemeral=True, thinking=True)
            data = await self.api.games(season, week)
            await interaction.followup.send(embed=week_embed(data), view=MatchupView(data["games"]), ephemeral=True)

        @self.tree.command(name="matchup", description="Look up a game by its NFL game ID")
        @app_commands.guild_only()
        async def matchup(interaction: discord.Interaction, game_id: str):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.show_game(interaction, game_id)

        @matchup.autocomplete("game_id")
        async def game_choices(interaction, current):
            try:
                data = await asyncio.wait_for(self.api.games(), timeout=2)
                return [app_commands.Choice(name=f"{g['away']} at {g['home']} · {g['game_id']}", value=g["game_id"])
                        for g in data["games"] if current.lower() in f"{g['home']} {g['away']} {g['game_id']}".lower()][:25]
            except Exception:
                return []

        @self.tree.command(name="nextgame", description="Show predictions for the next NFL game")
        @app_commands.guild_only()
        async def nextgame(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            feed = await self.api.upcoming(hours=168)
            if not feed["games"]:
                await interaction.followup.send("No confirmed NFL kickoff in the next seven days. Try /weeklymatchup.", ephemeral=True)
                return
            await self.show_game(interaction, feed["games"][0]["game_id"])

        @self.tree.command(name="botstatus", description="Check the prediction backend and this server's alert settings")
        @app_commands.guild_only()
        async def botstatus(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            setting = self.store.settings(interaction.guild_id)
            details = "Automatic alerts are disabled."
            if setting and setting["enabled"]:
                details = f"Posts {setting['minutes']} minutes before kickoff in <#{setting['channel_id']}>."
            try:
                await self.api.get("/healthz")
                details += "\nPrediction API is reachable."
            except Exception:
                details += "\nPrediction API is currently unavailable."
            details += "\nLast scheduler check " + timestamp(self.scheduler.last_checked.isoformat() if self.scheduler.last_checked else None)
            if self.scheduler.last_error:
                details += "\n" + self.scheduler.last_error
            last = self.store.last_delivery(interaction.guild_id)
            if last:
                outcome = "Delivered" if last["message_id"] else f"Pending · {last['error'] or 'checking delivery'}"
                details += f"\nLatest alert · {last['game_id']} · {outcome}"
            await interaction.followup.send(details, ephemeral=True)

        @self.tree.command(name="help", description="Explain the NFL bot commands and forecasts")
        @app_commands.guild_only()
        async def help_command(interaction: discord.Interaction):
            await interaction.response.send_message(
                "🥩 **Beef Kirkey’s menu**\n"
                "🏈 **NFL** · Pick a game with /weeklymatchup or see this week’s parlay board with /parlay.\n"
                "📋 **NFL paper desk** · /picks for plays, /account for the balance, /performance for results.\n"
                "₿ **BTC paper desk** · /crypto-trades for the play-by-play, /crypto-stats for the win rate "
                "and patterns, /crypto-account for the balance.\n"
                "🎲 **Polymarket Up/Down desk** · /updown-trades, /updown-stats, /updown-account.\n"
                "🥇 **Gold desk** · /gold-picks, /gold-results, /gold-stats, /gold-account "
                "(channel: /paper-channels set gold).\n"
                "**Player yardage** · /player-stats for a rushing or passing forecast. "
                "/pygraph for team scoring or player yardage history with a CSV.\n"
                "🛢️ **Oil desk** · /oil leaderboard compares five WTI models; /oil picks, results, account, "
                "stats, info (channel: /paper-channels set oil).\n"
                "🔴 **Oil D live** · Administrators can use /oil-live chart, channel, status, or off.\n"
                "**Oil D paper test** · /oilpaperlivetest graph or status. Route fills with /oilpaperlivetestchannel.\n"
                "🔥 **Gas desk** · /gas leaderboard, picks, results, account, stats, info "
                "(channel: /paper-channels set gas).\n"
                "🎖️ **Chief Beef Kirkey** · /chiefkirkey status, trades, bot, fullroster, alerts track every "
                "paper bot (feed channel: /paper-channels set chief).\n"
                "📣 **Server managers** · /paper-channels setup picks the BTC and parlay channels. "
                "Choose the player yardage channel with /paper-channels set players. "
                "/paper-channels show lists them. /alerts setup turns on pregame predictions.",
                ephemeral=True)

        alerts = app_commands.Group(name="alerts", description="Configure this server's pregame prediction posts",
                                    guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

        @alerts.command(name="setup", description="Post both models before each kickoff in the selected channel")
        @app_commands.checks.has_permissions(manage_guild=True)
        async def setup(interaction: discord.Interaction, channel: discord.TextChannel,
                        minutes: app_commands.Range[int, 5, 120] = 30):
            if channel.guild.id != interaction.guild_id:
                await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
                return
            permissions = channel.permissions_for(interaction.guild.me)
            missing = [name.replace("_", " ") for name in ALERT_PERMISSIONS if not getattr(permissions, name)]
            if missing:
                await interaction.response.send_message("The bot needs these channel permissions: " + ", ".join(missing), ephemeral=True)
                return
            self.store.configure(interaction.guild_id, channel.id, minutes)
            await interaction.response.send_message(
                f"Pregame predictions will post in {channel.mention} {minutes} minutes before kickoff. "
                "Any upcoming games already within that window will be posted on the next check.", ephemeral=True)

        @alerts.command(name="disable", description="Disable automatic pregame posts for this server")
        @app_commands.checks.has_permissions(manage_guild=True)
        async def disable(interaction: discord.Interaction):
            self.store.disable(interaction.guild_id)
            await interaction.response.send_message("Automatic pregame posts are disabled for this server.", ephemeral=True)

        self.tree.add_command(alerts)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token_path = os.getenv("DISCORD_TOKEN_FILE")
    token = Path(token_path).read_text(encoding="utf-8-sig").strip() if token_path else os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set DISCORD_TOKEN_FILE to the private file containing your bot token.")
    api = PredictionAPI(os.getenv("NFL_API_URL", "http://127.0.0.1:8000"))
    store = Store(os.getenv("DISCORD_STATE_PATH", "data/discord.sqlite3"))
    guild_id = int(os.environ["DISCORD_GUILD_ID"]) if os.getenv("DISCORD_GUILD_ID") else None
    application_id = int(os.environ["DISCORD_APPLICATION_ID"]) if os.getenv("DISCORD_APPLICATION_ID") else None
    NFLBot(api, store, guild_id, application_id).run(token, log_handler=None)


if __name__ == "__main__":
    main()
