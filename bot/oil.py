"""Discord side of the energy desks: 🛢️ WTI crude (``/oil``) and 🔥 natural gas (``/gas``).

* Each desk's channel (``/paper-channels set oil`` / ``set gas``) gets the **lead model's**
  pick, win, loss, out, take-profit, stop and out-before-news cards, plus **news alerts**:
  high/medium releases 30 minutes ahead, and detected price shocks or possible contract switches.
* ``/oil leaderboard`` (``/gas leaderboard``) compares every model's paper account; the other
  commands take an optional model (default: the lead model).
"""
from dataclasses import dataclass
import logging

import discord
from discord import app_commands

from .paper import _money, publication_embed
from .updown import Desk, SIDE, _cents, _time, trade_row, updown_account_card, updown_stats_card, updown_trade_card

logger = logging.getLogger(__name__)
MODEL_CHOICES = [("A", "A · Random walk"), ("B", "B · Fast volatility"), ("C", "C · Trend follower"),
                 ("D", "D · Market blend"), ("E", "E · News-aware")]
IMPACT = {"high": "🔴", "medium": "🟠", "low": "⚪"}


@dataclass(frozen=True)
class EnergyDesk:
    cards: Desk  # name, emoji, footer, api method, channel kind, trade cursor table
    info_cursors: str
    group: str  # slash command group
    commodity: str  # "WTI crude" | "Natural gas"
    market: str  # "WTI" | "Nat gas"
    news_note: str

    async def call(self, api, path, **params):
        return await getattr(api, self.cards.api)(path, **params)


OIL = EnergyDesk(Desk("Oil", "🛢️", "🥩 Beef Kirkey · WTI crude paper trading", "oil", "oil", "oil_cursors"),
                 "oil_info_cursors", "oil", "WTI crude", "WTI",
                 "EIA petroleum report (Wed), API inventory (Tue), jobs report, rig count")
GAS = EnergyDesk(Desk("Gas", "🔥", "🥩 Beef Kirkey · natural gas paper trading", "gas", "gas", "gas_cursors"),
                 "gas_info_cursors", "gas", "Natural gas", "Nat gas",
                 "EIA storage report (Thu), NOAA weather outlooks (daily 3 PM ET), rig count")
DESKS = (OIL, GAS)


def leaderboard_card(data, desk: EnergyDesk = OIL):
    rows = []
    for i, m in enumerate(data["models"], 1):
        rec = f"{m['wins']}W-{m['losses']}L" + (f" · {m['outs']} out" if m["outs"] else "") + \
              (f" · {m['targets']} TP" if m["targets"] else "") + (f" · {m['stops']} stop" if m["stops"] else "")
        brier = (f" · accuracy {m['brier_model']:.3f} vs market {m['brier_market']:.3f}"
                 if m.get("brier_model") is not None and m.get("brier_market") is not None else "")
        roi = f" ({m['roi']:+.1%})" if m.get("roi") is not None else ""
        rows.append((f"{i}. {m['model']} · {m['name']}" + (" ⭐ lead" if m["lead"] else ""),
                     f"**{_money(m['balance'])}** · {m['bets']} bets · {rec}{roi} · {m['open_count']} open{brier}\n"
                     f"_{m['idea']}_"))
    return {"title": f"{desk.cards.emoji} {desk.cards.name} model leaderboard (paper, $100 each)", "fields": rows,
            "footer": desk.cards.footer,
            "description": f"Same {desk.commodity} windows, same risk rules; only the fair-value model differs. "
                           "Accuracy is the Brier score (lower is better; coin flip 0.25)."}


def info_card(data, desk: EnergyDesk = OIL):
    upcoming = [f"{IMPACT.get(e['impact'], '•')} {e['at_et']} · {e['name']}" for e in data["upcoming"]
                if e["impact"] != "low"][:10]
    detected = [f"⚡ {e['at'][11:16]}Z · {e['name']}" for e in data["detected_last_24h"][:6]]
    fm = data["front_month"]
    return {"title": f"{desk.cards.emoji} {desk.cards.name} desk · what it's watching", "footer": desk.cards.footer,
            "fields": [
                ("Scheduled news ahead", "\n".join(upcoming) or "Nothing high- or medium-impact in the next 48 hours."),
                ("Detected in the last 24 h", "\n".join(detected) or "No price shocks or contract-switch jumps."),
                ("Front futures month", f"{fm['contract']} · last trading day {fm['last_trading_day']}. {fm['note']}"),
                ("How news is used", f"Watches: {desk.news_note}. No new entries 2 min before to 1 min after a "
                                     "high-impact release; open bets sell 3 min ahead if the bid gets the entry "
                                     "back. Model E also prices the extra move a release can add. There is no live "
                                     "headline feed — surprises show up through the price.")]}


def info_alert(event, desk: EnergyDesk = OIL):
    if event["kind"] == "detected":
        return {"title": f"⚡ {desk.cards.name} desk · {event['name']}", "footer": desk.cards.footer,
                "description": f"New {desk.commodity.lower()} entries pause briefly; the news-aware model prices "
                               "extra volatility."}
    return {"title": f"📰 Coming up · {event['name']}", "footer": desk.cards.footer,
            "description": f"{IMPACT.get(event['impact'], '')} {event['impact']} impact at {_time(event['at'])}. "
                           f"The {desk.cards.name.lower()} desk holds off new entries around it and may sell open "
                           "bets just before."}


class EnergyPublisher:
    def __init__(self, api, posts, publisher, desk: EnergyDesk = OIL):
        self.api, self.posts, self.publisher, self.desk = api, posts, publisher, desk

    async def tick(self):
        d = self.desk
        routes = self.posts.desk_routes(d.cards.route_kind, d.cards.cursor_table)
        if not routes:
            return
        try:
            lead = (await d.call(self.api, "/models"))["lead_model"]
        except Exception as exc:
            logger.warning("%s leaderboard unavailable (%s)", d.cards.name, type(exc).__name__)
            return
        for route in routes:
            g = route["guild_id"]
            try:
                info_cursor = next((r["event_id"] for r in self.posts.desk_routes(d.cards.route_kind, d.info_cursors)
                                    if r["guild_id"] == g), None)
                if route["event_id"] is None or info_cursor is None:  # new channel: start from now
                    acct = await d.call(self.api, f"/{lead}/account")
                    info = await d.call(self.api, "/info/events", after=0, limit=1)
                    self.posts.advance_desk(d.cards.cursor_table, g, acct["latest_id"])
                    self.posts.advance_desk(d.info_cursors, g, info["latest_id"])
                    continue
                data = await d.call(self.api, f"/{lead}/events", after=route["event_id"], limit=25)
                for event in data["events"]:
                    trade = event["trade"]
                    pid = f"{d.group}:{lead}:{trade['id']}:{event['kind']}"
                    await self.publisher.deliver(route, {"publication_id": pid, "revision": 1,
                                                         "payload": updown_trade_card(trade, d.cards)})
                    self.posts.advance_desk(d.cards.cursor_table, g, event["id"])
                news = await d.call(self.api, "/info/events", after=info_cursor, limit=25)
                for event in news["events"]:
                    await self.publisher.deliver(route, {"publication_id": f"{d.group}:info:{event['id']}",
                                                         "revision": 1, "payload": info_alert(event, d)})
                    self.posts.advance_desk(d.info_cursors, g, event["id"])
            except Exception as exc:
                logger.warning("%s publication for guild %s failed (%s: %s)", d.cards.name, g, type(exc).__name__, exc)


def OilPublisher(api, posts, publisher):  # noqa: N802 - kept for the oil desk's original name
    return EnergyPublisher(api, posts, publisher, OIL)


def register_energy_commands(bot, desk: EnergyDesk):
    d = desk
    group = app_commands.Group(name=d.group, description=f"{d.commodity} Up/Down paper desk: five competing models",
                               guild_only=True)
    choices = [app_commands.Choice(name=label, value=value) for value, label in MODEL_CHOICES]

    async def send(interaction, card):
        await interaction.followup.send(embed=publication_embed(card), ephemeral=True)

    async def lead_or(model):
        return model.value if model else (await d.call(bot.api, "/models"))["lead_model"]

    @group.command(name="leaderboard", description=f"Every {d.commodity.lower()} model's paper account side by side")
    async def leaderboard(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, leaderboard_card(await d.call(bot.api, "/models"), d))

    @group.command(name="picks", description=f"Open {d.commodity.lower()} picks (lead model unless you choose one)")
    @app_commands.choices(model=choices)
    async def picks(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        acct = await d.call(bot.api, f"/{m}/account")
        fields = [(f"{SIDE.get(t['side'])} {d.market} {t.get('horizon')} · ends {_time(t['window_end'])}",
                   f"{t['remaining']} @ {_cents(t['entry_price'])} · model {t['fair_side']:.0%} · stake "
                   f"{_money(t['cost'])}" + (f" · 🎯 {_cents(t['take_profit_price'])} / 🛑 {_cents(t['stop_loss_price'])}"
                                             if t.get("take_profit_price") else ""))
                  for t in acct["open_trades"]]
        latest = acct.get("latest_decisions") or []
        desc = f"Model {m} · {acct['model_name']} · " + (f"{len(fields)} open" if fields else "no open picks")
        if not fields and latest:
            desc += "\n" + "\n".join(f"{x['asset']} · {x['reason']}" for x in latest)
        await send(interaction, {"title": f"{d.cards.emoji} {d.cards.name} picks", "description": desc,
                                 "fields": fields, "footer": d.cards.footer})

    @group.command(name="results", description="Recent bets: wins, losses, outs (lead model unless chosen)")
    @app_commands.choices(model=choices)
    async def results(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        rows = [trade_row(t) for t in (await d.call(bot.api, f"/{m}/trades", limit=12))["trades"]]
        await send(interaction, {"title": f"{d.cards.emoji} {d.cards.name} results · model {m}", "footer": d.cards.footer,
                                 "description": "\n".join(rows) or f"No {d.commodity.lower()} paper bets yet."})

    @group.command(name="account", description="One model's paper balance, open bets and risk limits")
    @app_commands.choices(model=choices)
    async def account(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        acct = await d.call(bot.api, f"/{m}/account")
        acct["risk_settings"] = {**(await d.call(bot.api, "/models"))["risk_settings"], "markets": []}
        card = updown_account_card(acct, d.cards)
        card["title"] = f"{d.cards.emoji} {d.cards.name} paper account · model {m} · {acct['model_name']}"
        await send(interaction, card)

    @group.command(name="stats", description="One model's results and accuracy vs the market")
    @app_commands.choices(model=choices)
    async def stats(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        card = updown_stats_card(await d.call(bot.api, f"/{m}/stats"), d.cards)
        card["title"] = f"📒 {d.cards.name} scorecard · model {m}"
        await send(interaction, card)

    @group.command(name="info", description="News the desk is watching: releases ahead, shocks, contract month")
    async def info(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, info_card(await d.call(bot.api, "/info"), d))

    bot.tree.add_command(group)
    return group


def register_oil_commands(bot):
    return register_energy_commands(bot, OIL)


def register_gas_commands(bot):
    return register_energy_commands(bot, GAS)
