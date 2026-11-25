"""Publish Oil D live account updates and expose its Discord pause control."""

import asyncio
import logging
from typing import Literal

import discord
from discord import app_commands

from .graphs import oil_live_graph
from .messages import timestamp
from .paper import _money, channel_issue, publication_embed

logger = logging.getLogger(__name__)
ROUTE = "oil-live"
CURSORS = "oil_live_cursors"
FOOTER = "🔴 LIVE MONEY · Oil D · WTI 15 min"


def price_text(value):
    return f"{float(value) * 100:.4f}".rstrip("0").rstrip(".") + "¢"


def status_card(data, channel_id):
    state = data.get("state")
    message = data.get("message") or ""
    if data.get("paused"):
        entries = "Paused by operator"
        if state == "paused":
            state = data.get("worker_state") or state
            worker_message = data.get("worker_message")
            if worker_message and worker_message != message:
                message = "\n".join(filter(None, (message, worker_message)))
    elif state in ("offline", "blocked"):
        entries = "Unavailable"
    else:
        entries = "Enabled in configuration" if data.get("enabled") else "Disabled in configuration"
    fields = [("New entries", entries)]
    if data.get("paused") and data.get("disabled_reason"):
        fields.append(("Pause reason", data["disabled_reason"]))
    if state:
        fields.append(("Worker state", state.replace("_", " ").capitalize()))
    if data.get("last_heartbeat"):
        fields.append(("Last worker check", timestamp(data["last_heartbeat"], "R")))
    if data.get("enabled") and not data.get("active_trade") and "entry_contract" in data:
        check = "Verified for this window." if data["entry_contract"] else "Waiting to verify the current contract."
        fields.append(("Contract check", check))
    mode = {"rest": "REST", "stream_first": "WebSocket first · REST fallback",
            "stream_end_to_end": "WebSocket · signals and live entries"}.get(data.get("book_mode"))
    if mode:
        fields.append(("Quote source", mode))
    if data.get("stream_signal_check") and data.get("signal_source") == "stream":
        fields.append(("Latest signal check", data["stream_signal_check"].replace("_", " ").capitalize()))
    if channel_id:
        fields.append(("Order updates", f"<#{channel_id}>"))
    missed = data.get("last_missed_entry") or {}
    if missed.get("message"):
        previous = missed.get("signal_at") or missed.get("at")
        timing = f"{timestamp(previous, 'R')} · " if previous else ""
        fields.append((missed.get("title", "Last missed entry"), timing + missed["message"]))
    estimated = data.get("pnl_estimated") or data.get("fees_exact") is False
    net_label = "Lifetime live net P&L" if data.get("pilot_run") else "Live net P&L"
    if estimated:
        net_label += " (estimated fees)"
    for key, label in (("buying_power", "Exchange buying power"), ("exposure", "Live exposure"),
                       ("net_pnl", net_label)):
        if data.get(key) is not None:
            fields.append((label, _money(data[key])))
    if data.get("pilot_run") and data.get("pilot_net_pnl") is not None and data.get("pilot_equity") is not None:
        fields.append(("Current pilot", f"Started {_money(data['pilot_run']['starting_budget'])} · "
                       f"Realized balance {_money(data['pilot_equity'])} · P&L {_money(data['pilot_net_pnl'])}"))
    if data.get("trades") is not None:
        fields.append(("Closed live trades", data["trades"]))
    if data.get("matched_trades") and all(data.get(k) is not None for k in ("paired_live_net_pnl", "paper_net_pnl")):
        fields.append((f"Live vs. paper · {data['matched_trades']} matched",
                       f"Live {_money(data['paired_live_net_pnl'])} · Paper {_money(data['paper_net_pnl'])}"))
    if data.get("matched_trades") == 0 and data.get("signal_source") != "stream":
        fields.append(("Live vs. paper", "Waiting for a matched result."))
    limits = [f"{_money(data[key])} {label}" for key, label in (("pilot_budget", "pilot budget"),
              ("entry_cap", "max per entry"), ("loss_cap", "loss limit")) if data.get(key) is not None]
    if limits:
        fields.append(("Pilot limits", " · ".join(limits)))
    if (position := data.get("position")) and position.get("quantity") is not None:
        details = [f"{position['quantity']} {position.get('side', '')} contracts".strip()]
        for key, label in (("average_price", "avg entry"), ("take_profit", "take-profit limit"), ("stop", "stop trigger")):
            if position.get(key) is not None:
                details.append(f"{label} {price_text(position[key])}")
        fields.append(("Open position", " · ".join(details)))
    return {"title": "🔴 Oil D live status", "footer": FOOTER, "description": message, "fields": fields}


def event_card(event):
    order = event.get("order") or {}
    request = order.get("request") or {}
    execution = order.get("payload") or {}
    kind = event["kind"]
    quantity, price = event.get("quantity"), event.get("price")
    if kind == "fill":
        quantity = execution.get("filled") if quantity is None else quantity
        price = execution.get("average_price") if price is None else price
    else:
        quantity = request.get("quantity") if quantity is None else quantity
        price = request.get("price") if price is None else price
    phase = event.get("phase") or order.get("kind")
    state = event.get("state") or order.get("state")
    side = event.get("side") or request.get("side")
    fields = []
    if event.get("at"):
        fields.append(("Recorded time", timestamp(event["at"], "f")))
    if event.get("slug"):
        fields.append(("Contract", event["slug"]))
    if side:
        fields.append(("Side", side))
    if phase:
        fields.append(("Order phase", str(phase).replace("_", " ").capitalize()))
    if state:
        fields.append(("Order state", str(state).removeprefix("ORDER_STATE_").replace("_", " ").capitalize()))
    if quantity is not None:
        fields.append(("Contracts", quantity))
    if price is not None:
        fields.append(("Price per contract", price_text(price)))
    if event.get("net_pnl") is not None:
        label = "Net P&L (estimated fees)" if event.get("fees_exact") is False else "Net P&L"
        fields.append((label, _money(event["net_pnl"])))
    title = event.get("title") or kind.replace("_", " ").capitalize()
    return {"title": f"🔴 Oil D live · {title}", "footer": FOOTER,
            "description": event.get("message"), "fields": fields}


class OilLivePublisher:
    def __init__(self, api, posts, publisher, guild_id):
        self.api, self.posts, self.publisher, self.guild_id = api, posts, publisher, guild_id

    async def tick(self):
        route = next((r for r in self.posts.desk_routes(ROUTE, CURSORS) if r["guild_id"] == self.guild_id), None)
        if not route or route["event_id"] is None:
            return
        feed = await self.api.oil_live("/events", after=route["event_id"], limit=25)
        for event in feed["events"]:
            if event["kind"] != "order_prepared" and event.get("state") != "sending":
                publication_id = f"{ROUTE}:{event['id']}@{route['channel_id']}"
                await self.publisher.deliver(route, {"publication_id": publication_id, "revision": 1,
                                                     "payload": event_card(event)})
            self.posts.advance_desk(CURSORS, self.guild_id, event["id"])


class OilLivePermissionRequired(app_commands.CheckFailure):
    pass


def register_oil_live_commands(bot):
    group = app_commands.Group(name="oil-live", description="Configure and monitor Oil D live trading",
                               guild_only=True, default_permissions=discord.Permissions(administrator=True))

    def live_admin(interaction):
        if not bot.guild_id or interaction.guild_id != bot.guild_id or not interaction.permissions.administrator:
            raise OilLivePermissionRequired("Oil D live commands require Administrator in the configured server.")
        return True

    @group.command(name="channel", description="Set the channel for Oil D live orders and alerts")
    @app_commands.check(live_admin)
    async def channel(interaction: discord.Interaction, destination: discord.TextChannel):
        if issue := channel_issue(interaction, destination):
            await interaction.response.send_message(issue, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.oil_live("/status")
        bot.paper_posts.configure_oil_live(interaction.guild_id, destination.id, data["latest_id"])
        card = status_card(data, destination.id)
        route = {"guild_id": interaction.guild_id, "channel_id": destination.id}
        try:
            await bot.paper_publisher.deliver(route, {"publication_id": f"{ROUTE}:channel:{interaction.id}",
                                                     "revision": 1, "payload": card})
        except Exception as exc:
            logger.warning("Oil D live status publication failed (%s)", type(exc).__name__)
            await interaction.followup.send(
                f"Order updates are set to {destination.mention}. Discord could not post the initial status.",
                ephemeral=True)
        else:
            await interaction.followup.send(f"Oil D live orders and alerts will post in {destination.mention}.",
                                            ephemeral=True)

    @group.command(name="status", description="See Oil D live worker status, exposure, and results")
    @app_commands.check(live_admin)
    async def status(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.oil_live("/status")
        channel_id = bot.paper_posts.channels(interaction.guild_id).get(ROUTE)
        await interaction.followup.send(embed=publication_embed(status_card(data, channel_id)), ephemeral=True)

    @group.command(name="chart", description="Chart the Oil D live pilot's realized balance and net P&L over time")
    @app_commands.describe(time="Time range to display in Arizona time")
    @app_commands.check(live_admin)
    async def chart(interaction: discord.Interaction,
                    time: Literal["1h", "6h", "12h", "24h", "7d", "30d", "all"] = "24h"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.oil_live("/chart", time=time)
        if not data["available"]:
            await interaction.followup.send(data["message"], ephemeral=True)
            return
        png = await asyncio.to_thread(oil_live_graph, data)
        note = (f"Starting budget **{_money(data['starting_budget'])}** · Realized balance "
                f"**{_money(data['balance'])}** · Pilot P&L **{_money(data['pilot_pnl'])}**\n"
                f"This range ({time}) · {_money(data['range_pnl'])} realized across {data['closed_in_range']} closes.")
        if data["estimated_fees_count"]:
            note += "\nSome closed trades use estimated fees."
        if data["open_trade"]:
            note += "\nAn active trade is awaiting its final result."
        if data["budget_source"] == "legacy_heartbeat":
            note += "\nLegacy journal uses the worker’s recorded pilot budget as its starting value."
        await interaction.followup.send(content=note, file=discord.File(png, filename="oil-live-chart.png"),
                                        ephemeral=True)

    @group.command(name="off", description="Pause new Oil D live entries")
    @app_commands.check(live_admin)
    async def off(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await bot.api.oil_live("/off", body={"actor": str(interaction.user.id)})
        except Exception as exc:
            logger.warning("Oil D live pause could not be confirmed (%s)", type(exc).__name__)
            await interaction.followup.send(
                "Could not confirm the pause. Check /oil-live status and retry if entries are still enabled.",
                ephemeral=True)
            return
        await interaction.followup.send("Oil D live entries paused. Exit monitoring and settlement continue.",
                                        ephemeral=True)

    bot.tree.add_command(group)
