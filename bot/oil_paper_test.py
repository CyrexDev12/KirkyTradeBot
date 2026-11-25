"""Discord monitoring for the separate Oil D execution paper account."""
import asyncio
import logging
from typing import Literal

import discord
from discord import app_commands

from .graphs import oil_live_graph
from .messages import timestamp
from .oil_live import OilLivePermissionRequired, price_text
from .paper import _money, channel_issue, publication_embed

logger = logging.getLogger(__name__)
ROUTE = "oil-paper-test"
CURSORS = "oil_paper_test_cursors"
FOOTER = "PAPER ONLY · Oil D execution test · Simulated orders and fills"
EVENT_TITLES = {
    "entry_gate": "Entry eligibility changed", "status": "Worker status changed",
    "entry_sent": "Entry order sent", "entry_filled": "Entry filled",
    "entry_partial": "Entry partially filled", "entry_unfilled": "Entry unfilled",
    "entry_unverified": "Entry could not be verified", "exit_filled": "Exit filled",
    "exit_unfilled": "Exit unfilled", "settlement": "Settlement",
    "trade_closed": "Trade closed", "state_changed": "Trade state changed",
}


def _label(value):
    return str(value).replace("_", " ")


def _account_fields(data):
    return [(label, _money(data[key])) for key, label in (("balance", "Realized paper balance"),
            ("realized_pnl", "Account realized P&L")) if data.get(key) is not None]


def _entries(data):
    return "Eligible while live entries are paused" if data["entries_allowed"] else "On hold"


def status_card(data, channel_id):
    fields = []
    if data.get("entries_allowed") is not None:
        fields.append(("New paper entries", _entries(data)))
    if data.get("state"):
        fields.append(("Worker", _label(data["state"]).capitalize()))
    if data.get("last_heartbeat"):
        fields.append(("Last worker check", timestamp(data["last_heartbeat"], "R")))
    if data.get("starting_budget") is not None:
        fields.append(("Starting paper budget", _money(data["starting_budget"])))
    if data.get("available", True):
        fields.extend(_account_fields(data))
    if data.get("available", True) and data.get("buying_power") is not None:
        fields.append(("Available paper buying power", _money(data["buying_power"])))
    limits = [f"{_money(data[key])} {name}" for key, name in (("entry_cap", "per entry"), ("loss_cap", "realized loss limit"))
              if data.get(key) is not None]
    if limits:
        fields.append(("Paper limits", " · ".join(limits)))
    if active := data.get("active"):
        details = [str(active.get("slug") or ""), str(active.get("side") or ""), _label(active["state"])]
        if active.get("remaining") is not None:
            details.append(f"{active['remaining']} contracts remaining")
        fields.append(("Active order or position", " · ".join(filter(None, details))))
    if channel_id:
        fields.append(("Paper updates", f"<#{channel_id}>"))
    return {"title": "Oil D paper test · Account status", "footer": FOOTER, "fields": fields,
            "description": _label(data.get("message") or data.get("worker_reason") or "")}


def event_card(event):
    kind = event["kind"]
    fields = []
    if event.get("at"):
        fields.append(("Recorded time", timestamp(event["at"], "f")))
    for key, label in (("slug", "Contract"), ("side", "Side"), ("quantity", "Contracts")):
        if event.get(key) is not None:
            fields.append((label, _label(event[key])))
    if kind in ("entry_gate", "status") and event.get("state"):
        fields.append(("Worker state", _label(event["state"])))
    if kind.startswith("entry_") and event.get("requested_quantity") is not None:
        fields.append(("Entry quantity requested", str(event["requested_quantity"])))
    if kind in ("exit_filled", "exit_unfilled") and event.get("exit_kind"):
        fields.append(("Exit order", _label(event["exit_kind"])))
    if event.get("entries_allowed") is not None:
        fields.append(("New paper entries", _entries(event)))
    for key, label in (("limit_price", "Order limit"), ("price", "Fill price per contract")):
        if key == "limit_price" and kind == "trade_closed":
            continue
        if event.get(key) is not None:
            fields.append((label, price_text(event[key])))
    for key, label in (("fee", "Modeled fee"), ("proceeds", "Proceeds"), ("net_pnl", "Trade net P&L")):
        if event.get(key) is not None:
            fields.append((label, _money(event[key])))
    if event.get("coverage"):
        fields.append(("Quote coverage", _label(event["coverage"])))
    fields.extend(_account_fields(event))
    title = EVENT_TITLES.get(kind, _label(kind).capitalize())
    if kind == "state_changed" and event.get("exit_kind"):
        title = _label(event["exit_kind"]).capitalize()
    return {"title": f"Oil D paper test · {title}",
            "description": _label(event.get("reason") or event.get("message") or ""), "footer": FOOTER,
            "fields": fields}


def _availability(data):
    if not data.get("last_heartbeat"):
        return "waiting"
    return "offline" if data.get("state") == "offline" else "online"


class OilPaperTestPublisher:
    def __init__(self, api, posts, publisher, guild_id):
        self.api, self.posts, self.publisher, self.guild_id = api, posts, publisher, guild_id
        self.lock = asyncio.Lock()

    async def _notices(self, route):
        for notice_id, card in self.posts.oil_paper_test_notices(self.guild_id, route["channel_id"]):
            await self.publisher.deliver(route, {"publication_id": f"{ROUTE}:status:{notice_id}", "revision": 1,
                                                 "payload": card})
            self.posts.finish_oil_paper_test_notice(notice_id)

    async def configure(self, channel_id):
        async with self.lock:
            data = await self.api.oil_paper_test("/status")
            self.posts.observe_oil_paper_test(self.guild_id, channel_id, _availability(data),
                                              status_card(data, channel_id), subscribe_after=data["latest_id"])
            try:
                await self._notices({"guild_id": self.guild_id, "channel_id": channel_id})
            except Exception as exc:
                logger.warning("Oil D paper test initial status delivery failed (%s)", type(exc).__name__)
                return False
            return True

    async def tick(self):
        async with self.lock:
            route = next((r for r in self.posts.desk_routes(ROUTE, CURSORS) if r["guild_id"] == self.guild_id), None)
            if not route or route["event_id"] is None:
                return
            await self._notices(route)
            feed = await self.api.oil_paper_test("/events", after=route["event_id"], limit=25)
            for event in feed["events"]:
                await self.publisher.deliver(route, {"publication_id": f"{ROUTE}:{event['id']}",
                                                     "revision": 1, "payload": event_card(event)})
                self.posts.advance_desk(CURSORS, self.guild_id, event["id"])
            # The journal records the gate and normal worker transitions. A worker that stops
            # heartbeating cannot record its own outage, so publish those availability changes here.
            data = await self.api.oil_paper_test("/status")
            self.posts.observe_oil_paper_test(self.guild_id, route["channel_id"], _availability(data),
                                              status_card(data, route["channel_id"]))
            await self._notices(route)


def register_oil_paper_test_commands(bot):
    def admin(interaction):
        if not bot.guild_id or interaction.guild_id != bot.guild_id or not interaction.permissions.administrator:
            raise OilLivePermissionRequired("Oil D paper test commands require Administrator in the configured server.")
        return True

    @bot.tree.command(name="oilpaperlivetestchannel", description="Set the channel for simulated Oil D orders and fills")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.check(admin)
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if issue := channel_issue(interaction, channel):
            await interaction.response.send_message(issue, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        posted = await bot.oil_paper_test_publisher.configure(channel.id)
        message = f"Oil D paper test updates will post in {channel.mention}."
        if not posted:
            message += " The initial account snapshot is queued for retry."
        await interaction.followup.send(message, ephemeral=True)

    group = app_commands.Group(name="oilpaperlivetest", description="Monitor the Oil D execution paper account",
                               guild_only=True, default_permissions=discord.Permissions(administrator=True))

    @group.command(name="status", description="See paper test entry eligibility, buying power and realized P&L")
    @app_commands.check(admin)
    async def status(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.oil_paper_test("/status")
        channel_id = bot.paper_posts.channels(interaction.guild_id).get(ROUTE)
        await interaction.followup.send(embed=publication_embed(status_card(data, channel_id)), ephemeral=True)

    @group.command(name="graph", description="Graph the Oil D paper test balance and realized P&L in Arizona time")
    @app_commands.describe(time="Time range to display (default 24h)")
    @app_commands.check(admin)
    async def graph(interaction: discord.Interaction,
                    time: Literal["1h", "6h", "12h", "24h", "7d", "30d", "all"] = "24h"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.oil_paper_test("/chart", time=time)
        if not data["available"]:
            await interaction.followup.send(data["message"], ephemeral=True)
            return
        png = await asyncio.to_thread(oil_live_graph, data, title="PAPER ONLY · Oil D execution test")
        note = (f"**PAPER ONLY · Oil D execution test**\nStarting budget {_money(data['starting_budget'])} · "
                f"Realized balance {_money(data['balance'])} · Account P&L {_money(data['pilot_pnl'])}\n"
                f"This range ({time}) · {_money(data['range_pnl'])} realized across {data['closed_in_range']} closes.")
        if data["open_trade"]:
            note += "\nAn active paper order or position is awaiting its final result."
        await interaction.followup.send(content=note, file=discord.File(png, filename="oil-paper-test.png"),
                                        ephemeral=True)

    bot.tree.add_command(group)
