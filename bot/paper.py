"""Discord surface for the NFL paper-trading account.

Everything shown comes from the backend's stored plans, journal and account
calculations (``/api/paper/*``); the bot formats it deterministically and never
creates numbers of its own. Weekly plans, trade cards, lifecycle alerts and
account reports arrive through the backend's durable outbox and are posted with
stored message IDs, so a crash during a send is reconciled against channel
history before anything is sent again.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from pathlib import Path
import sqlite3

import discord
from discord import app_commands

from .messages import timestamp

logger = logging.getLogger(__name__)
KINDS = ("picks", "updates", "reports", "parlay", "crypto", "gold", "players", "chief", "oil")
MARKER = "pub "
ROUTE_NAMES = {"crypto": "₿ BTC + crypto Up/Down paper trades", "gold": "🥇 Gold Up/Down paper picks",
               "parlay": "🏈 Weekly NFL parlays", "players": "NFL player yardage picks",
               "chief": "🎖️ Chief Beef Kirkey (all-bot status + trade tracker)",
               "oil": "🛢️ WTI crude Up/Down paper picks + oil news",
               "picks": "NFL paper picks", "updates": "NFL trade updates", "reports": "NFL account reports"}


def utcnow():
    return datetime.now(timezone.utc)


class PaperPosts:
    """Per-guild channel routing and per-publication delivery records (bot-side state)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_channels (guild_id INTEGER NOT NULL, kind TEXT NOT NULL,
                    channel_id INTEGER NOT NULL, PRIMARY KEY (guild_id, kind));
                CREATE TABLE IF NOT EXISTS paper_posts (guild_id INTEGER NOT NULL, publication_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER,
                    started_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, error TEXT,
                    PRIMARY KEY (guild_id, publication_id));
                CREATE TABLE IF NOT EXISTS crypto_cursors (guild_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS updown_cursors (guild_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS gold_cursors (guild_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS oil_cursors (guild_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS oil_info_cursors (guild_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def set_channel(self, guild_id, kind, channel_id):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO paper_channels VALUES (?,?,?)", (guild_id, kind, channel_id))

    def routes(self, kind):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM paper_channels WHERE kind=?", (kind,))]

    def channels(self, guild_id):
        with closing(self.connect()) as db:
            return {r["kind"]: r["channel_id"] for r in
                    db.execute("SELECT * FROM paper_channels WHERE guild_id=?", (guild_id,))}

    def configured_nfl_routes(self):
        with closing(self.connect()) as db:
            return [r["kind"] for r in db.execute("SELECT DISTINCT kind FROM paper_channels "
                                                 "WHERE kind IN ('picks','updates','reports','parlay')")]

    def configure_crypto(self, guild_id, channel_id, latest_event_id, *, parlay_channel_id=None):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO paper_channels VALUES (?,'crypto',?)", (guild_id, channel_id))
            if parlay_channel_id is not None:
                db.execute("INSERT OR REPLACE INTO paper_channels VALUES (?,'parlay',?)", (guild_id, parlay_channel_id))
            # First subscription starts with the next event. Switching channels keeps any pending deliveries.
            db.execute("INSERT OR IGNORE INTO crypto_cursors VALUES (?,?)", (guild_id, latest_event_id))

    def crypto_routes(self):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(
                "SELECT guild_id, channel_id, event_id FROM paper_channels JOIN crypto_cursors USING (guild_id) "
                "WHERE kind='crypto'")]

    CURSOR_TABLES = ("updown_cursors", "gold_cursors", "oil_cursors", "oil_info_cursors")

    def desk_routes(self, kind, table):
        """Routes for an Up/Down desk (crypto cards share the crypto channel, gold has its own).
        The cursor is None until the publisher first sees the route, so a new channel starts fresh."""
        if table not in self.CURSOR_TABLES:
            raise ValueError(table)
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(
                f"SELECT c.guild_id, c.channel_id, u.event_id FROM paper_channels c "
                f"LEFT JOIN {table} u USING (guild_id) WHERE c.kind=?", (kind,))]

    def advance_desk(self, table, guild_id, event_id):
        if table not in self.CURSOR_TABLES:
            raise ValueError(table)
        with closing(self.connect()) as db, db:
            db.execute(f"INSERT INTO {table} VALUES (?,?) ON CONFLICT(guild_id) "
                       "DO UPDATE SET event_id=max(event_id, excluded.event_id)", (guild_id, event_id))

    def updown_routes(self):
        return self.desk_routes("crypto", "updown_cursors")

    def advance_updown(self, guild_id, event_id):
        self.advance_desk("updown_cursors", guild_id, event_id)

    def advance_crypto(self, guild_id, event_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE crypto_cursors SET event_id=max(event_id, ?) WHERE guild_id=?", (event_id, guild_id))

    def post(self, guild_id, publication_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM paper_posts WHERE guild_id=? AND publication_id=?",
                             (guild_id, publication_id)).fetchone()
            return dict(row) if row else None

    def begin(self, guild_id, publication_id, revision, channel_id, now):
        """Record intent before sending, so an ambiguous outcome can be reconciled after a crash."""
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO paper_posts (guild_id,publication_id,revision,channel_id,started_at)
                          VALUES (?,?,?,?,?) ON CONFLICT(guild_id,publication_id) DO UPDATE SET
                          revision=excluded.revision, attempts=paper_posts.attempts+1""",
                       (guild_id, publication_id, revision, channel_id, now.isoformat()))
        return self.post(guild_id, publication_id)

    def done(self, guild_id, publication_id, revision, message_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE paper_posts SET message_id=?, revision=?, error=NULL WHERE guild_id=? AND publication_id=?",
                       (message_id, revision, guild_id, publication_id))

    def failed(self, guild_id, publication_id, error):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE paper_posts SET error=? WHERE guild_id=? AND publication_id=?",
                       (error, guild_id, publication_id))


def nonce(publication_id, guild_id):
    return hashlib.sha256(f"{guild_id}:{publication_id}".encode()).hexdigest()[:24]


def receipt(publication_id):
    return "#" + hashlib.sha256(publication_id.encode()).hexdigest()[:12]


def publication_embed(payload: dict, publication_id: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=payload["title"][:256], description=(payload.get("description") or "")[:4000],
                          colour=0x5EA5E0)
    for name, value in (payload.get("fields") or [])[:20]:
        embed.add_field(name=str(name)[:256], value=str(value)[:1024] or "—", inline=False)
    footer = payload.get("footer") or "PAPER TRADING"
    if publication_id:
        footer += f" · {receipt(publication_id)}"
    embed.set_footer(text=footer[:2048])
    return embed


class PaperPublisher:
    def __init__(self, api, posts: PaperPosts, transport, clock=utcnow):
        self.api, self.posts, self.transport, self.clock = api, posts, transport, clock
        self.last_error = None

    async def tick(self):
        channels = self.posts.configured_nfl_routes()
        if not channels:
            return
        items = (await self.api.paper_outbox(channels=channels)).get("publications", [])
        for item in items:
            routes = self.posts.routes(item["channel"])
            if not routes:
                continue  # leave pending until a channel is configured
            ids, errors = [], []
            for route in routes:
                try:
                    ids.append(str(await self.deliver(route, item)))
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    self.posts.failed(route["guild_id"], item["publication_id"], type(exc).__name__)
                    logger.warning("Paper publication %s failed (%s)", item["publication_id"], type(exc).__name__)
            if errors:
                self.last_error = f"Publication delivery failed ({', '.join(errors)})"
                await self.api.paper_ack(item["publication_id"], item["revision"], None, ",".join(errors))
            elif item.get("status") != "delivered":
                await self.api.paper_ack(item["publication_id"], item["revision"], ",".join(ids), None)

    async def deliver(self, route, item):
        guild_id, pid, revision = route["guild_id"], item["publication_id"], item["revision"]
        previous = self.posts.post(guild_id, pid)
        # A newly chosen parlay channel gets the current card, even when the
        # previous destination already received this week's publication.
        if item.get("kind") == "weekly_parlay" and previous and previous["channel_id"] != route["channel_id"]:
            pid = f"{pid}@{route['channel_id']}"
            previous = self.posts.post(guild_id, pid)
        embed = publication_embed(item["payload"], pid)
        if previous and previous["message_id"]:
            if previous["revision"] >= revision:
                return previous["message_id"]
            # Lifecycle update of an existing card: edit in place, never repost.
            await self.transport.edit(previous["channel_id"], previous["message_id"], embed)
            self.posts.done(guild_id, pid, revision, previous["message_id"])
            return previous["message_id"]
        if previous:  # an earlier attempt may have reached Discord
            existing = await self.transport.find_existing(previous["channel_id"], pid,
                                                          datetime.fromisoformat(previous["started_at"]))
            if existing:
                if previous["revision"] < revision:
                    await self.transport.edit(previous["channel_id"], existing, embed)
                self.posts.done(guild_id, pid, revision, existing)
                return existing
        row = self.posts.begin(guild_id, pid, revision, route["channel_id"], self.clock())
        message_id = await self.transport.send(row["channel_id"], embed, nonce(pid, guild_id))
        self.posts.done(guild_id, pid, revision, message_id)
        return message_id


class PaperTransport:
    def __init__(self, client):
        self.client = client

    async def channel(self, channel_id):
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Paper trading posts require a server text channel")
        return channel

    async def find_existing(self, channel_id, publication_id, started_at):
        channel = await self.channel(channel_id)
        markers = (f" · {receipt(publication_id)}", f" · {MARKER}{publication_id}")
        async for message in channel.history(limit=None, after=started_at - timedelta(seconds=5)):
            if message.author.id == self.client.user.id and any(
                    (e.footer.text or "").endswith(markers) for e in message.embeds):
                return message.id
        return None

    async def send(self, channel_id, embed, nonce_value):
        channel = await self.channel(channel_id)
        message = await channel.send(embed=embed, nonce=nonce_value, allowed_mentions=discord.AllowedMentions.none())
        return message.id

    async def edit(self, channel_id, message_id, embed):
        channel = await self.channel(channel_id)
        message = await channel.fetch_message(message_id)
        await message.edit(embed=embed)


def _money(value):
    v = float(value)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def account_text(a: dict) -> str:
    lines = [f"🏈 **NFL paper account · {_money(a['equity'])}**",
             f"Available {_money(a['available_cash'])} · set aside for orders {_money(a['reserved'])}",
             f"Closed trades {_money(a['realized_pnl'])} · open trades {_money(a['unrealized_pnl'])} in gains/losses"]
    for p in a["positions"]:
        lines.append(f"• {p['selected_outcome']} · {p['quantity']} shares worth {_money(p['net_liquidation_value'])} "
                     f"· `{p['trade_id']}`")
    for w in a["working_entries"]:
        lines.append(f"• Waiting to buy {w['remaining']} shares at {float(w['limit']) * 100:.0f}¢ · `{w['trade_id']}`")
    if a.get("paused"):
        lines.append(f"New entries paused: {a.get('pause_reason')}. Exits are still monitored.")
    if a.get("provisional"):
        lines.append("⏳ Some prices are delayed. The account value may change.")
    return "\n".join(lines)[:1900]


def register_paper_commands(bot, posts: PaperPosts):
    tree, api = bot.tree, bot.api

    async def reply(interaction, text=None, embed=None):
        await interaction.followup.send(text, embed=embed, ephemeral=True)

    @tree.command(name="parlay", description="See Kirkey’s weekly NFL parlays, leg odds, hit chances and payout multipliers")
    @app_commands.guild_only()
    async def parlay(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/parlay")
        if data.get("card"):
            await reply(interaction, embed=publication_embed(data["card"]))
        else:
            await reply(interaction, data.get("message") or "This week’s parlays are still cooking. 🥩")

    @tree.command(name="picks", description="This week's paper-trading plans and their status")
    @app_commands.guild_only()
    async def picks(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/picks")
        if not data.get("plan_key"):
            await reply(interaction, data.get("message") or "No weekly plan yet.")
            return
        s = data["summary"]
        lines = [s["headline"], f"Paper account **{_money(s['equity'])}**"]
        for t in data["trades"]:
            total = f" {t['total_line']}" if t.get("total_line") is not None else ""
            game = t["game_id"].split("_")
            matchup = f"{game[2]} at {game[3]}" if len(game) == 4 else t["game_id"]
            lines.append(f"• {matchup} · **{t['selected_outcome']}{total}** · {t['status'].replace('_', ' ').lower()} · "
                         f"entry {float(t['entry_limit']) * 100:.0f}¢ → target {float(t['target_limit']) * 100:.0f}¢ "
                         f"/ stop {float(t['stop_trigger']) * 100:.0f}¢ · `{t['trade_id']}`")
        await reply(interaction, embed=discord.Embed(title=f"🏈 Kirkey’s paper picks · Week {data['week']}, {data['season']}",
                                                     description="\n".join(lines)[:4000], colour=0x5EA5E0))

    @tree.command(name="trade", description="See a paper pick’s prices, grade and recent activity")
    @app_commands.guild_only()
    async def trade(interaction: discord.Interaction, trade_id: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper(f"/trades/{trade_id}")
        history = "\n".join(f"<t:{int(datetime.fromisoformat(e['ts']).timestamp())}:t> · "
                            f"{e['type'].replace('_', ' ').lower()}" for e in data["history"][-5:])
        card = dict(data["card"])
        if history:
            card["fields"] = list(card["fields"]) + [("Recent activity", history)]
        await reply(interaction, embed=publication_embed(card))

    @tree.command(name="account", description="Paper account cash, reservations, equity and open positions")
    @app_commands.guild_only()
    async def account(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await reply(interaction, account_text(await api.paper("/account")))

    @tree.command(name="performance", description="Paper returns, drawdown, fees, fill and exit statistics")
    @app_commands.guild_only()
    async def performance(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/performance")
        p, a = data["performance"], data["account"]
        change = float(a["equity"]) - float(a["starting_equity"])
        record = (f"{p['wins']}/{p['closed_trades']} closed trades won ({p['wins']/p['closed_trades']:.0%})"
                  if p["closed_trades"] else "Waiting for the first closed trade.")
        text = (f"📋 **NFL paper scorecard · {_money(a['equity'])}**\n"
                f"{record}\nChange since start **{_money(change)}** · fees {_money(p['fees'])}\n"
                f"Largest drop from a previous high {float(p['max_drawdown']):.1%}")
        await reply(interaction, text)

    @tree.command(name="why-no-trades", description="See what kept NFL picks off the board")
    @app_commands.guild_only()
    async def why_no_trades(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/why-no-trades")
        r = data["readiness"]
        lines = ["🏈 **NFL pick check**", (data.get("summary") or {}).get("headline") or
                 "Waiting for the first weekly scan."]
        failures = (data.get("gate_failures") or {}).values()
        for info in list(failures)[:3]:
            lines.append(f"• {info['count']} skipped · {info['meaning']}")
        if not r["uncertainty_profiles"]:
            lines.append("The model’s error estimates are still being prepared.")
        await reply(interaction, "\n".join(lines)[:1900])

    @tree.command(name="health", description="Paper worker, feed freshness and pending posts")
    @app_commands.guild_only()
    async def health(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/health")
        worker = data.get("worker") or {}
        ages = data.get("feed_age_seconds") or {}
        newest = min(ages.values()) if ages else None
        planning = worker.get("planning") or {}
        lines = ["🏈 **NFL paper desk**" + (" · entries paused" if data["paused"] else ""),
                 f"Last check {timestamp(worker['last_heartbeat'], 'R')}" if worker.get("last_heartbeat")
                 else "Waiting for the first worker check."]
        if planning:
            lines.append(f"Scanning games · {planning.get('games_completed', 0)}/{planning.get('games_total', 0)} "
                         f"checked · {planning.get('books_received', 0)}/{planning.get('books_requested', 0)} prices received")
        if newest is not None:
            lines.append(f"Freshest market quote {newest:.0f}s ago · {len(ages)} markets tracked")
        if data["last_planning_run"]:
            lines.append(f"Last plan {timestamp(data['last_planning_run'], 'R')}")
        lines.append(f"{data['pending_publications']} posts waiting to go out.")
        for label, key in (("Latest issue", "last_error"), ("Market data issue", "venue_error")):
            if worker.get(key):
                lines.append(f"{label} · {str(worker[key])[:300]}")
        await reply(interaction, "\n".join(lines))

    @tree.command(name="paper-pause", description="Stop new paper entries (exits keep running)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def paper_pause(interaction: discord.Interaction, reason: str = "operator pause"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await api.paper_operator("/pause", {"actor": str(interaction.user.id), "reason": reason})
        await reply(interaction, "New paper entries paused. Exit monitoring and settlement continue.")

    @tree.command(name="paper-resume", description="Resume new paper entries")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def paper_resume(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await api.paper_operator("/resume", {"actor": str(interaction.user.id)})
        await reply(interaction, "New paper entries resumed.")

    group = app_commands.Group(name="paper-channels", description="Choose where paper picks and updates are posted",
                               guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    def channel_issue(interaction, channel):
        if channel.guild.id != interaction.guild_id:
            return "Choose a text channel in this server."
        perms = channel.permissions_for(interaction.guild.me)
        missing = [n.replace("_", " ") for n in ("view_channel", "send_messages", "embed_links", "read_message_history")
                   if not getattr(perms, n)]
        if missing:
            return f"To post in {channel.mention}, I need " + ", ".join(missing) + "."

    @group.command(name="setup", description="Pick the channels for BTC trades and the weekly NFL parlay board")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_channels(interaction: discord.Interaction, crypto: discord.TextChannel, parlay: discord.TextChannel):
        for channel in (crypto, parlay):
            issue = channel_issue(interaction, channel)
            if issue:
                await interaction.response.send_message(issue, ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        account = await api.crypto("/account")
        posts.configure_crypto(interaction.guild_id, crypto.id, account["latest_id"], parlay_channel_id=parlay.id)
        await reply(interaction, f"🥩 Grill’s ready!\n₿ BTC entries, exits and skips → {crypto.mention}"
                                 f"\n🏈 Weekly parlay board → {parlay.mention}")

    @group.command(name="show", description="See which channels receive Beef Kirkey's updates")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def show_channels(interaction: discord.Interaction):
        configured = posts.channels(interaction.guild_id)
        lines = [name + " → " + (f"<#{configured[kind]}>" if kind in configured else "Choose a channel")
                 for kind, name in ROUTE_NAMES.items()]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @group.command(name="set", description="Choose a Discord channel for a paper publication")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in KINDS])
    async def set_channel(interaction: discord.Interaction, kind: app_commands.Choice[str], channel: discord.TextChannel):
        issue = channel_issue(interaction, channel)
        if issue:
            await interaction.response.send_message(issue, ephemeral=True)
            return
        if kind.value == "crypto":
            await interaction.response.defer(ephemeral=True, thinking=True)
            account = await api.crypto("/account")
            posts.configure_crypto(interaction.guild_id, channel.id, account["latest_id"])
            await reply(interaction, f"₿ BTC paper entries, exits and skips will post in {channel.mention}.")
            return
        posts.set_channel(interaction.guild_id, kind.value, channel.id)
        if kind.value == "gold":
            await interaction.response.send_message(
                f"🥇 Gold picks, wins, losses and outs will post in {channel.mention}. "
                "See open picks with /gold-picks and results with /gold-results.", ephemeral=True)
            return
        if kind.value == "parlay":
            await interaction.response.send_message(f"🏈 Weekly parlay boards will post in {channel.mention}. "
                                                    "See the latest with /parlay.", ephemeral=True)
            return
        if kind.value == "oil":
            await interaction.response.send_message(
                f"🛢️ WTI crude picks, wins, losses, outs and oil news alerts will post in {channel.mention}. "
                "Compare the models with /oil leaderboard.", ephemeral=True)
            return
        if kind.value == "chief":
            await interaction.response.send_message(
                f"🎖️ Chief Beef Kirkey will track every bot in {channel.mention}: every new trade and result, "
                "alerts when a bot goes down or hits its loss limit, and status reports at 8 AM and 8 PM Arizona. "
                "Use /chiefkirkey status any time.", ephemeral=True)
            return
        if kind.value == "players":
            await interaction.response.send_message(f"Weekly player yardage picks will post in {channel.mention}. "
                                                    "Use /player-stats to check a forecast.", ephemeral=True)
            return
        await interaction.response.send_message(f"Paper {kind.value} will post in {channel.mention}.", ephemeral=True)

    tree.add_command(group)
