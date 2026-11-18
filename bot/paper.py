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

logger = logging.getLogger(__name__)
KINDS = ("picks", "updates", "reports")
MARKER = "pub "


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


def publication_embed(payload: dict, publication_id: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=payload["title"][:256], description=(payload.get("description") or "")[:4000],
                          colour=0x5EA5E0)
    for name, value in (payload.get("fields") or [])[:20]:
        embed.add_field(name=str(name)[:256], value=str(value)[:1024] or "—", inline=False)
    footer = payload.get("footer") or "PAPER TRADING"
    if publication_id:
        footer += f" · {MARKER}{publication_id}"
    embed.set_footer(text=footer[:2048])
    return embed


class PaperPublisher:
    def __init__(self, api, posts: PaperPosts, transport, clock=utcnow):
        self.api, self.posts, self.transport, self.clock = api, posts, transport, clock
        self.last_error = None

    async def tick(self):
        items = (await self.api.paper_outbox()).get("publications", [])
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
            else:
                await self.api.paper_ack(item["publication_id"], item["revision"], ",".join(ids), None)

    async def deliver(self, route, item):
        guild_id, pid, revision = route["guild_id"], item["publication_id"], item["revision"]
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
        marker = f"{MARKER}{publication_id}"
        async for message in channel.history(limit=None, after=started_at - timedelta(seconds=5)):
            if message.author.id == self.client.user.id and any(
                    (e.footer.text or "").endswith(marker) for e in message.embeds):
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
    lines = [f"**PAPER ACCOUNT** · equity **{_money(a['equity'])}** (started {_money(a['starting_equity'])})",
             f"Cash {_money(a['cash'])} · reserved {_money(a['reserved'])} · available {_money(a['available_cash'])}",
             f"Open liquidation value {_money(a['open_liquidation_value'])} · realized {_money(a['realized_pnl'])}"
             f" · unrealized {_money(a['unrealized_pnl'])} · fees {_money(a['fees_paid'])}"]
    for p in a["positions"]:
        lines.append(f"• `{p['trade_id']}` {p['selected_outcome']} {p['status']} qty {p['quantity']} · "
                     f"cost {_money(p['cost_basis'])} · net liq {_money(p['net_liquidation_value'])}"
                     + (" (provisional)" if p["provisional"] else ""))
    for w in a["working_entries"]:
        lines.append(f"• `{w['trade_id']}` working entry (not a position) · {w['remaining']} @ "
                     f"{float(w['limit']) * 100:.0f}¢ · reserved {_money(w['reserved'])}")
    if a.get("paused"):
        lines.append(f"New entries paused: {a.get('pause_reason')}. Exits are still monitored.")
    if a.get("provisional"):
        lines.append("Some marks are stale or incomplete; values are provisional.")
    return "\n".join(lines)[:1900]


def register_paper_commands(bot, posts: PaperPosts):
    tree, api = bot.tree, bot.api

    async def reply(interaction, text=None, embed=None):
        await interaction.followup.send(text, embed=embed, ephemeral=True)

    @tree.command(name="picks", description="This week's paper-trading plans and their status")
    @app_commands.guild_only()
    async def picks(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/picks")
        if not data.get("plan_key"):
            await reply(interaction, data.get("message") or "No weekly plan yet.")
            return
        s = data["summary"]
        lines = [s["headline"], f"{s['qualifying_trades']} qualifying · equity {_money(s['equity'])}"]
        for t in data["trades"]:
            lines.append(f"• `{t['trade_id']}` {t['selected_outcome']} {t['market_type']} · **{t['status']}** · "
                         f"entry {float(t['entry_limit']) * 100:.0f}¢ → target {float(t['target_limit']) * 100:.0f}¢ "
                         f"/ stop {float(t['stop_trigger']) * 100:.0f}¢ · qty {t['quantity']}")
        await reply(interaction, embed=discord.Embed(title=f"PAPER PICKS · {data['plan_key']}",
                                                     description="\n".join(lines)[:4000], colour=0x5EA5E0))

    @tree.command(name="trade", description="Exact terms, model values, rating components and history of a paper trade")
    @app_commands.guild_only()
    async def trade(interaction: discord.Interaction, trade_id: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper(f"/trades/{trade_id}")
        history = "\n".join(f"{e['ts'][:16]} {e['type']}" for e in data["history"][-10:]) or "No paper orders yet."
        card = dict(data["card"])
        card["fields"] = list(card["fields"]) + [("History", history)]
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
        text = (f"**PAPER PERFORMANCE** · equity {_money(a['equity'])} ({_money(change)} since $100 start)\n"
                f"Closed {p['closed_trades']} · wins {p['wins']} · losses {p['losses']} · expectancy "
                f"{p['expectancy'] or '—'} · max drawdown {float(p['max_drawdown']):.1%}\n"
                f"Entries {p['entries']} · any fill {p['entries_any_fill']} · full fill {p['entries_full_fill']}\n"
                f"Exits {p['exit_outcomes'] or 'none'} · fees {_money(p['fees'])} · data outages {p['outages']}\n"
                "Past paper results are not evidence of future profitability.")
        await reply(interaction, text)

    @tree.command(name="why-no-trades", description="Gate failures and data-readiness status for the paper strategy")
    @app_commands.guild_only()
    async def why_no_trades(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/why-no-trades")
        r = data["readiness"]
        lines = [f"Recorded market-path examples: {r['path_examples']}",
                 f"Forward validation: {'passed' if (r['validation'] or {}).get('passed') else 'not passed / not run'}",
                 f"Model-uncertainty profiles: {'present' if r['uncertainty_profiles'] else 'missing'}"]
        if data.get("summary"):
            lines.insert(0, data["summary"]["headline"])
        for code, info in list((data.get("gate_failures") or {}).items())[:8]:
            lines.append(f"• {code} ×{info['count']} — {info['meaning']}")
        await reply(interaction, "\n".join(lines)[:1900])

    @tree.command(name="health", description="Paper worker, feed freshness and pending posts")
    @app_commands.guild_only()
    async def health(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await api.paper("/health")
        worker = data.get("worker") or {}
        ages = data.get("feed_age_seconds") or {}
        newest = min(ages.values()) if ages else None
        await reply(interaction,
                    f"Mode {data['mode']} · worker last loop {worker.get('last_loop') or 'never'}\n"
                    f"Freshest book age {f'{newest:.0f}s' if newest is not None else '—'} across {len(ages)} markets\n"
                    f"Pending posts {data['pending_publications']} · last planning run "
                    f"{data['last_planning_run'] or 'none'} · paused {data['paused']}\n"
                    f"Last worker error: {worker.get('last_error') or 'none'}")

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

    group = app_commands.Group(name="paper-channels", description="Route paper-trading posts to channels",
                               guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @group.command(name="set", description="Choose the channel for weekly picks, trade updates or account reports")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in KINDS])
    async def set_channel(interaction: discord.Interaction, kind: app_commands.Choice[str], channel: discord.TextChannel):
        if channel.guild.id != interaction.guild_id:
            await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        missing = [n for n in ("view_channel", "send_messages", "embed_links", "read_message_history")
                   if not getattr(perms, n)]
        if missing:
            await interaction.response.send_message("The bot needs: " + ", ".join(missing), ephemeral=True)
            return
        posts.set_channel(interaction.guild_id, kind.value, channel.id)
        await interaction.response.send_message(f"Paper {kind.value} will post in {channel.mention}.", ephemeral=True)

    tree.add_command(group)
