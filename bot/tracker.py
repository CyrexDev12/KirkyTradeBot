"""Chief Beef Kirkey: the tracker that follows every other bot.

It reads ``/api/tracker`` (all paper desks: NFL, BTC spot, crypto Up/Down, gold) and

* posts a **trade feed** to its channel: every new open and every win / loss / out /
  take-profit / stop, across all bots, batched into one card per poll;
* posts an **alert** when a bot goes down, starts erroring, hits its daily loss
  limit, or recovers;
* posts a **status report** of all bots at the hours in ``CHIEF_REPORT_HOURS``
  (Arizona time, default ``8,20``);
* answers ``/chiefkirkey status`` (every bot), ``/chiefkirkey trades [bot]``,
  ``/chiefkirkey bot <bot>`` (one bot in depth), ``/chiefkirkey fullroster [bot] [show]``
  (every trade by every bot, as clean tables) and ``/chiefkirkey alerts``.

Channel: ``/paper-channels set chief #channel``. A new channel starts from the
current state, so it never floods old trades.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import logging
import os
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .paper import _money, publication_embed

logger = logging.getLogger(__name__)
PHOENIX = ZoneInfo("America/Phoenix")
NAME = "Chief Beef Kirkey"
FOOTER = "🎖️ Chief Beef Kirkey · tracking every Beef Kirkey paper bot"
LIGHT = {"ok": "🟢", "warn": "🟡", "down": "🔴"}
STATUS_ICON = {"OPEN": "🎟️", "WORKING": "⏳", "WIN": "✅", "LOSS": "❌", "OUT": "💸", "TARGET": "🎯",
               "STOP": "🛑", "FLAT": "➖"}
FEED_LINES = 12


def report_hours(env=None):
    raw = (env if env is not None else os.environ).get("CHIEF_REPORT_HOURS", "8,20")
    return tuple(sorted({int(h) for h in raw.split(",") if h.strip() != ""}))


def _signed(x):
    return ("+" if x > 0 else "") + _money(x) if x is not None else "—"


def _age(seconds):
    if seconds is None:
        return "no heartbeat"
    if seconds < 90:
        return f"heartbeat {int(seconds)}s ago"
    return f"heartbeat {int(seconds // 60)} min ago"


def desk_line(d):
    if d.get("balance") is None:
        return "; ".join(d.get("notes") or []) or "no data"
    parts = [f"**{_money(d['balance'])}**"]
    if d.get("starting_balance"):
        parts[0] += f" ({_signed(d['balance'] - d['starting_balance'])} all-time)"
    if d.get("today_pnl") is not None:
        parts.append(f"today {_signed(d['today_pnl'])}")
    parts.append(f"{d.get('open_count', 0)} open" + (f" ({_money(d['at_risk'])} at risk)" if d.get("at_risk") else ""))
    rec = d.get("record") or {}
    if rec.get("closed"):
        parts.append(f"{rec['wins']}W-{rec['losses']}L recent")
    text = " · ".join(parts) + f"\n{_age(d.get('heartbeat_age_s'))}"
    if d.get("notes"):
        text += " · " + " · ".join(d["notes"][:2])
    return text[:1000]


def status_card(status, title=None):
    t = status["totals"]
    states = t["states"]
    head = (f"{LIGHT['ok']} {states['ok']} healthy · {LIGHT['warn']} {states['warn']} warning · "
            f"{LIGHT['down']} {states['down']} down\n"
            f"Combined paper balance **{_money(t['balance'])}** · today {_signed(t['today_pnl'])} · "
            f"{t['open_count']} open ({_money(t['at_risk'])} at risk)")
    return {"title": title or f"🎖️ {NAME} · bot status", "description": head, "footer": FOOTER,
            "fields": [(f"{LIGHT[d['state']]} {d['emoji']} {d['name']}", desk_line(d)) for d in status["desks"]]}


def trade_line(t, emoji):
    icon = STATUS_ICON.get(t["status"], "•")
    text = f"{icon} **{t['status']}** {emoji} {t['market']} {t.get('side') or ''}"
    if t["status"] in ("OPEN", "WORKING"):
        text += f" · stake {_money(t['stake'] or 0)}"
    else:
        text += f" · {_signed(t['pnl'])}"
    return text


def feed_card(trades, emojis):
    lines = [trade_line(t, emojis.get(t["desk"], "")) for t in trades[:FEED_LINES]]
    if len(trades) > FEED_LINES:
        lines.append(f"…and {len(trades) - FEED_LINES} more · /chief-trades")
    wins = sum(t["status"] == "WIN" for t in trades)
    losses = sum(t["status"] == "LOSS" for t in trades)
    title = f"📡 {NAME} · trade tracker"
    if wins or losses:
        title += f" · {wins}W {losses}L"
    return {"title": title, "description": "\n".join(lines), "footer": FOOTER}


def alert_card(desk, before):
    if desk["state"] == "ok":
        title = f"🟢 {desk['emoji']} {desk['name']} is back to normal"
    elif desk["state"] == "down":
        title = f"🔴 {desk['emoji']} {desk['name']} is down"
    else:
        title = f"🟡 {desk['emoji']} {desk['name']} needs attention"
    return {"title": title, "description": desk_line(desk) + (f"\n(was {before})" if before else ""),
            "footer": FOOTER}


def trade_key(t):
    return f"{t['desk']}:{t['id']}:{t['status']}"


class ChiefStore:
    """Per-guild memory: which trade states were already posted, last desk states, last report slot."""

    def __init__(self, posts):
        self.posts = posts
        with closing(posts.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS chief_seen (guild_id INTEGER NOT NULL, key TEXT NOT NULL,
                    PRIMARY KEY (guild_id, key));
                CREATE TABLE IF NOT EXISTS chief_state (guild_id INTEGER NOT NULL, desk TEXT NOT NULL,
                    state TEXT NOT NULL, PRIMARY KEY (guild_id, desk));
                CREATE TABLE IF NOT EXISTS chief_reports (guild_id INTEGER PRIMARY KEY, slot TEXT NOT NULL);
            """)

    def initialized(self, guild_id):
        with closing(self.posts.connect()) as db:
            return db.execute("SELECT 1 FROM chief_state WHERE guild_id=? LIMIT 1", (guild_id,)).fetchone() is not None

    def unseen(self, guild_id, keys):
        with closing(self.posts.connect()) as db:
            seen = {r[0] for r in db.execute("SELECT key FROM chief_seen WHERE guild_id=?", (guild_id,))}
        return [k for k in keys if k not in seen]

    def mark_seen(self, guild_id, keys):
        with closing(self.posts.connect()) as db, db:
            db.executemany("INSERT OR IGNORE INTO chief_seen VALUES (?,?)", [(guild_id, k) for k in keys])

    def states(self, guild_id):
        with closing(self.posts.connect()) as db:
            return dict(db.execute("SELECT desk, state FROM chief_state WHERE guild_id=?", (guild_id,)).fetchall())

    def set_states(self, guild_id, states):
        with closing(self.posts.connect()) as db, db:
            db.executemany("INSERT OR REPLACE INTO chief_state VALUES (?,?,?)",
                           [(guild_id, d, s) for d, s in states.items()])

    def last_report(self, guild_id):
        with closing(self.posts.connect()) as db:
            row = db.execute("SELECT slot FROM chief_reports WHERE guild_id=?", (guild_id,)).fetchone()
            return row[0] if row else None

    def set_report(self, guild_id, slot):
        with closing(self.posts.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO chief_reports VALUES (?,?)", (guild_id, slot))


class ChiefPublisher:
    def __init__(self, api, posts, publisher, clock=lambda: datetime.now(timezone.utc), hours=None):
        self.api, self.posts, self.publisher, self.clock = api, posts, publisher, clock
        self.store = ChiefStore(posts)
        self.hours = report_hours() if hours is None else hours

    async def _send(self, route, kind, card, unique):
        pid = f"chief:{kind}:{hashlib.sha1(unique.encode()).hexdigest()[:16]}"
        await self.publisher.deliver(route, {"publication_id": pid, "revision": 1, "payload": card})

    async def tick(self):
        routes = self.posts.routes("chief")
        if not routes:
            return
        try:
            status = await self.api.tracker("/status")
            trades = (await self.api.tracker("/trades", limit=60))["trades"]
        except Exception as exc:
            logger.warning("Chief tracker could not read the bots (%s)", type(exc).__name__)
            return
        emojis = {d["id"]: d["emoji"] for d in status["desks"]}
        now = self.clock().astimezone(PHOENIX)
        for route in routes:
            g = route["guild_id"]
            try:
                await self._route(route, g, status, trades, emojis, now)
            except Exception as exc:
                logger.warning("Chief publication for guild %s failed (%s)", g, type(exc).__name__)

    async def _route(self, route, g, status, trades, emojis, now):
        states = {d["id"]: d["state"] for d in status["desks"]}
        keys = [trade_key(t) for t in trades]
        if not self.store.initialized(g):
            # New channel: start from the current state and say hello with one report.
            self.store.mark_seen(g, keys)
            self.store.set_states(g, states)
            await self._send(route, "hello", status_card(status, f"🎖️ {NAME} reporting for duty"),
                             f"{g}:{status['as_of']}")
            return
        new = set(self.store.unseen(g, keys))
        fresh = [t for t in reversed(trades) if trade_key(t) in new]  # oldest first
        if fresh:
            await self._send(route, "feed", feed_card(fresh, emojis), f"{g}:" + ",".join(sorted(new)))
            self.store.mark_seen(g, [trade_key(t) for t in fresh])
        before = self.store.states(g)
        for d in status["desks"]:
            if before.get(d["id"]) not in (None, d["state"]):
                await self._send(route, "alert", alert_card(d, before.get(d["id"])),
                                 f"{g}:{d['id']}:{d['state']}:{status['as_of']}")
        self.store.set_states(g, states)
        if now.hour in self.hours:
            slot = now.strftime("%Y-%m-%d-%H")
            if self.store.last_report(g) != slot:
                when = now.strftime("%-I %p").lower()
                await self._send(route, "report", status_card(status, f"🎖️ {NAME} · {when} report"), f"{g}:{slot}")
                self.store.set_report(g, slot)


BOTS = [("nfl", "🏈 NFL Polymarket"), ("btc", "₿ BTC spot"), ("updown", "🎲 Crypto Up/Down"), ("gold", "🥇 Gold Up/Down"),
        ("oil-A", "🛢️ Oil A · Random walk"), ("oil-B", "🛢️ Oil B · Fast volatility"),
        ("oil-C", "🛢️ Oil C · Trend follower"), ("oil-D", "🛢️ Oil D · Market blend"), ("oil-E", "🛢️ Oil E · News-aware"),
        ("gas-A", "🔥 Gas A · Random walk"), ("gas-B", "🔥 Gas B · Fast volatility"),
        ("gas-C", "🔥 Gas C · Trend follower"), ("gas-D", "🔥 Gas D · Market blend"), ("gas-E", "🔥 Gas E · News-aware")]


def bot_card(status, trades, desk_id):
    desk = next(d for d in status["desks"] if d["id"] == desk_id)
    rows = [trade_line(t, desk["emoji"]) for t in trades if t["desk"] == desk_id][:10]
    fields = [("Status", desk_line(desk))]
    if desk.get("notes"):
        fields.append(("Notes", "\n".join(desk["notes"])[:1000]))
    fields.append(("Recent trades", "\n".join(rows) or "No paper trades yet."))
    return {"title": f"{LIGHT[desk['state']]} {desk['emoji']} {desk['name']}", "fields": fields, "footer": FOOTER}


def alerts_card(status):
    bad = [d for d in status["desks"] if d["state"] != "ok"]
    if not bad:
        return {"title": f"🟢 {NAME} · all bots healthy", "description": "No warnings or outages right now.",
                "footer": FOOTER}
    return {"title": f"🚨 {NAME} · {len(bad)} bot(s) need attention", "footer": FOOTER,
            "fields": [(f"{LIGHT[d['state']]} {d['emoji']} {d['name']}", desk_line(d)) for d in bad]}


ROSTER_LINES = 40  # rows per page (keeps each Discord message well under its 6,000-character limit)
ROSTER_PAGES = 12  # most pages one /chiefkirkey fullroster sends
SHOW = {"all": None, "open": ("OPEN", "WORKING"), "closed": ("WIN", "LOSS", "OUT", "TARGET", "STOP", "FLAT")}


def _when(at):
    if not at:
        return "—"
    try:
        ts = datetime.fromisoformat(str(at).replace("Z", "+00:00")).astimezone(PHOENIX)
    except ValueError:
        return "—"
    return ts.strftime("%m/%d %-I:%M") + ("a" if ts.hour < 12 else "p")


def _cell(text, width):
    text = str(text if text is not None else "—")
    return (text[: width - 1] + "…") if len(text) > width else text.ljust(width)


def _amount(x, signed=False):
    if x is None:
        return "—"
    sign = "+" if signed and x > 0 else "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def roster_row(t):
    pnl = _amount(t["pnl"], signed=True) if t["status"] not in ("OPEN", "WORKING") else "open"
    return (f"{_cell(t['status'], 7)} {_cell(t['market'], 22)} {_cell(t.get('side'), 5)} "
            f"{_amount(t.get('stake')).rjust(9)} {pnl.rjust(9)}  {_when(t.get('at'))}")


ROSTER_HEADER = f"{'RESULT':7} {'MARKET':22} {'SIDE':5} {'STAKE':>9} {'P&L':>9}  WHEN (AZ)"


def summary_text(s):
    parts = [f"{s['closed']} closed · {s['wins']}W {s['losses']}L {s['outs']} out" +
             (f" {s['targets']} take-profit" if s.get("targets") else "") +
             (f" {s['stops']} stopped" if s.get("stops") else "") +
             (f" {s['flat']} flat" if s["flat"] else ""), f"{s['open']} open"]
    parts.append(f"net {_amount(s['net_pnl'], signed=True)} on {_amount(s['staked'])} staked" +
                 (f" ({s['roi']:+.1%})" if s.get("roi") is not None else ""))
    if s.get("win_rate") is not None:
        parts.append(f"{s['win_rate']:.0%} win rate")
    return " · ".join(parts)


def roster_cards(data, show="all"):
    """Cards for /chiefkirkey fullroster: one summary, then each bot's trades as aligned tables."""
    keep = SHOW[show]
    t = data["totals"]
    head = {"title": f"🎖️ {NAME} · full roster" + ("" if show == "all" else f" · {show} trades"),
            "description": f"**All bots:** {summary_text(t)}", "footer": FOOTER,
            "fields": [(f"{LIGHT.get(d['state'], '•')} {d['emoji']} {d['name']}"
                        + (f" · {_money(d['balance'])}" if d.get("balance") is not None else ""),
                        summary_text(d["summary"])) for d in data["desks"]]}
    cards = [head]
    for d in data["desks"]:
        rows = [roster_row(x) for x in d["trades"] if keep is None or x["status"] in keep]
        if not rows:
            continue
        pages = [rows[i:i + ROSTER_LINES] for i in range(0, len(rows), ROSTER_LINES)]
        for n, page in enumerate(pages, 1):
            label = f" · page {n}/{len(pages)}" if len(pages) > 1 else ""
            cards.append({"title": f"{d['emoji']} {d['name']} · {len(rows)} trade(s){label}", "footer": FOOTER,
                          "description": "```\n" + ROSTER_HEADER + "\n" + "\n".join(page) + "\n```"})
    if len(cards) > ROSTER_PAGES:
        hidden = len(cards) - ROSTER_PAGES + 1
        cards = cards[:ROSTER_PAGES - 1] + [{"title": f"…{hidden} more page(s)", "footer": FOOTER,
                                             "description": "Narrow it with the bot or show options, or open "
                                                            "/api/tracker/roster for everything."}]
    return cards


def register_chief_commands(bot):
    """``/chiefkirkey status | trades | bot | fullroster | alerts``."""
    group = app_commands.Group(name="chiefkirkey", description="Chief Beef Kirkey: tracks every paper bot",
                               guild_only=True)
    choices = [app_commands.Choice(name=label, value=value) for value, label in BOTS]

    async def send(interaction, card):
        await interaction.followup.send(embed=publication_embed(card), ephemeral=True)

    @group.command(name="status", description="Status of every paper bot: health, balance, today, open risk")
    async def status_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, status_card(await bot.api.tracker("/status")))

    @group.command(name="trades", description="Latest trades across every paper bot, or one bot")
    @app_commands.describe(bot_name="Only show this bot's trades")
    @app_commands.rename(bot_name="bot")
    @app_commands.choices(bot_name=choices)
    async def trades_cmd(interaction: discord.Interaction, bot_name: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        status = await bot.api.tracker("/status")
        emojis = {d["id"]: d["emoji"] for d in status["desks"]}
        params = {"limit": 15, **({"desk": bot_name.value} if bot_name else {})}
        trades = (await bot.api.tracker("/trades", **params))["trades"]
        title = f"📡 {NAME} · latest trades" + (f" · {bot_name.name}" if bot_name else "")
        await send(interaction, {"title": title, "footer": FOOTER,
                                 "description": "\n".join(trade_line(t, emojis.get(t["desk"], "")) for t in trades)
                                 or "No paper trades yet."})

    @group.command(name="bot", description="Deep dive on one bot: health, notes and its recent trades")
    @app_commands.rename(bot_name="bot")
    @app_commands.choices(bot_name=choices)
    async def bot_cmd(interaction: discord.Interaction, bot_name: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        status = await bot.api.tracker("/status")
        trades = (await bot.api.tracker("/trades", limit=60, desk=bot_name.value))["trades"]
        await send(interaction, bot_card(status, trades, bot_name.value))

    @group.command(name="fullroster", description="Every trade by every bot, grouped by bot, with totals")
    @app_commands.describe(bot_name="Only this bot", show="All trades, only open ones, or only closed ones")
    @app_commands.rename(bot_name="bot")
    @app_commands.choices(bot_name=choices, show=[app_commands.Choice(name=n, value=n) for n in SHOW])
    async def roster_cmd(interaction: discord.Interaction, bot_name: app_commands.Choice[str] | None = None,
                         show: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        params = {"desk": bot_name.value} if bot_name else {}
        data = await bot.api.tracker("/roster", **params)
        for card in roster_cards(data, show.value if show else "all"):
            await interaction.followup.send(embed=publication_embed(card), ephemeral=True)

    @group.command(name="alerts", description="Only the bots that are down, erroring or at a loss limit")
    async def alerts_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, alerts_card(await bot.api.tracker("/status")))

    bot.tree.add_command(group)
