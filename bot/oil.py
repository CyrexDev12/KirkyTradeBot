"""Discord side of the WTI crude oil desk.

* The ``oil`` channel (``/paper-channels set oil``) gets the **lead model's** pick, win, loss,
  out, take-profit, stop and out-before-news cards, plus **oil news alerts**: high/medium
  releases 30 minutes ahead, and detected price shocks or possible contract switches.
* ``/oil leaderboard`` compares every model's paper account; the other commands take an
  optional model (default: the lead model).
"""
import logging

import discord
from discord import app_commands

from .paper import _money, publication_embed
from .updown import Desk, SIDE, _cents, _time, trade_row, updown_account_card, updown_stats_card, updown_trade_card

logger = logging.getLogger(__name__)
OIL = Desk("Oil", "🛢️", "🥩 Beef Kirkey · WTI crude paper trading", "oil", "oil", "oil_cursors")
MODEL_CHOICES = [("A", "A · Random walk"), ("B", "B · Fast volatility"), ("C", "C · Trend follower"),
                 ("D", "D · Market blend"), ("E", "E · News-aware")]
IMPACT = {"high": "🔴", "medium": "🟠", "low": "⚪"}


def leaderboard_card(data):
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
    return {"title": "🛢️ Oil model leaderboard (paper, $100 each)", "fields": rows, "footer": OIL.footer,
            "description": "Same WTI windows, same risk rules; only the fair-value model differs. "
                           "Accuracy is the Brier score (lower is better; coin flip 0.25)."}


def info_card(data):
    upcoming = [f"{IMPACT.get(e['impact'], '•')} {e['at_et']} · {e['name']}" for e in data["upcoming"]
                if e["impact"] != "low"][:10]
    detected = [f"⚡ {e['at'][11:16]}Z · {e['name']}" for e in data["detected_last_24h"][:6]]
    fm = data["front_month"]
    return {"title": "🛢️ Oil desk · what it's watching", "footer": OIL.footer, "fields": [
        ("Scheduled news ahead", "\n".join(upcoming) or "Nothing high-impact in the next 48 hours."),
        ("Detected in the last 24 h", "\n".join(detected) or "No price shocks or contract-switch jumps."),
        ("Front futures month", f"{fm['contract']} · last trading day {fm['last_trading_day']}. {fm['note']}"),
        ("How news is used", "No new entries 2 min before to 1 min after a high-impact release; open bets "
                             "sell 3 min ahead if the bid gets the entry back. Model E also prices the extra "
                             "move a release can add. There is no live headline feed — surprises show up "
                             "through the price.")]}


def info_alert(event):
    if event["kind"] == "detected":
        return {"title": f"⚡ Oil desk · {event['name']}", "footer": OIL.footer,
                "description": "New oil entries pause briefly; the news-aware model prices extra volatility."}
    return {"title": f"📰 Coming up · {event['name']}", "footer": OIL.footer,
            "description": f"{IMPACT.get(event['impact'], '')} {event['impact']} impact at {_time(event['at'])}. "
                           "The oil desk holds off new entries around it and may sell open bets just before."}


class OilPublisher:
    def __init__(self, api, posts, publisher):
        self.api, self.posts, self.publisher = api, posts, publisher

    async def tick(self):
        routes = self.posts.desk_routes("oil", "oil_cursors")
        if not routes:
            return
        try:
            lead = (await self.api.oil("/models"))["lead_model"]
        except Exception as exc:
            logger.warning("Oil leaderboard unavailable (%s)", type(exc).__name__)
            return
        for route in routes:
            g = route["guild_id"]
            try:
                info_cursor = next((r["event_id"] for r in self.posts.desk_routes("oil", "oil_info_cursors")
                                    if r["guild_id"] == g), None)
                if route["event_id"] is None or info_cursor is None:  # new channel: start from now
                    acct = await self.api.oil(f"/{lead}/account")
                    info = await self.api.oil("/info/events", after=0, limit=1)
                    self.posts.advance_desk("oil_cursors", g, acct["latest_id"])
                    self.posts.advance_desk("oil_info_cursors", g, info["latest_id"])
                    continue
                data = await self.api.oil(f"/{lead}/events", after=route["event_id"], limit=25)
                for event in data["events"]:
                    trade = event["trade"]
                    await self.publisher.deliver(route, {"publication_id": f"oil:{lead}:{trade['id']}:{event['kind']}",
                                                         "revision": 1, "payload": updown_trade_card(trade, OIL)})
                    self.posts.advance_desk("oil_cursors", g, event["id"])
                news = await self.api.oil("/info/events", after=info_cursor, limit=25)
                for event in news["events"]:
                    await self.publisher.deliver(route, {"publication_id": f"oil:info:{event['id']}", "revision": 1,
                                                         "payload": info_alert(event)})
                    self.posts.advance_desk("oil_info_cursors", g, event["id"])
            except Exception as exc:
                logger.warning("Oil publication for guild %s failed (%s: %s)", g, type(exc).__name__, exc)


def register_oil_commands(bot):
    group = app_commands.Group(name="oil", description="WTI crude Up/Down paper desk: five competing models",
                               guild_only=True)
    choices = [app_commands.Choice(name=label, value=value) for value, label in MODEL_CHOICES]

    async def send(interaction, card):
        await interaction.followup.send(embed=publication_embed(card), ephemeral=True)

    async def lead_or(model):
        return model.value if model else (await bot.api.oil("/models"))["lead_model"]

    @group.command(name="leaderboard", description="Every oil model's paper account side by side")
    async def leaderboard(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, leaderboard_card(await bot.api.oil("/models")))

    @group.command(name="picks", description="Open oil picks (lead model unless you choose one)")
    @app_commands.choices(model=choices)
    async def picks(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        acct = await bot.api.oil(f"/{m}/account")
        fields = [(f"{SIDE.get(t['side'])} WTI {t.get('horizon')} · ends {_time(t['window_end'])}",
                   f"{t['remaining']} @ {_cents(t['entry_price'])} · model {t['fair_side']:.0%} · stake "
                   f"{_money(t['cost'])}" + (f" · 🎯 {_cents(t['take_profit_price'])} / 🛑 {_cents(t['stop_loss_price'])}"
                                             if t.get("take_profit_price") else ""))
                  for t in acct["open_trades"]]
        latest = acct.get("latest_decisions") or []
        desc = f"Model {m} · {acct['model_name']} · " + (f"{len(fields)} open" if fields else "no open picks")
        if not fields and latest:
            desc += "\n" + "\n".join(f"{d['asset']} · {d['reason']}" for d in latest)
        await send(interaction, {"title": "🛢️ Oil picks", "description": desc, "fields": fields, "footer": OIL.footer})

    @group.command(name="results", description="Recent oil bets: wins, losses, outs (lead model unless chosen)")
    @app_commands.choices(model=choices)
    async def results(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        rows = [trade_row(t) for t in (await bot.api.oil(f"/{m}/trades", limit=12))["trades"]]
        await send(interaction, {"title": f"🛢️ Oil results · model {m}", "footer": OIL.footer,
                                 "description": "\n".join(rows) or "No oil paper bets yet."})

    @group.command(name="account", description="One oil model's paper balance, open bets and risk limits")
    @app_commands.choices(model=choices)
    async def account(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        acct = await bot.api.oil(f"/{m}/account")
        acct["risk_settings"] = {**(await bot.api.oil("/models"))["risk_settings"], "markets": []}
        card = updown_account_card(acct, OIL)
        card["title"] = f"🛢️ Oil paper account · model {m} · {acct['model_name']}"
        await send(interaction, card)

    @group.command(name="stats", description="One oil model's results and accuracy vs the market")
    @app_commands.choices(model=choices)
    async def stats(interaction: discord.Interaction, model: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        m = await lead_or(model)
        card = updown_stats_card(await bot.api.oil(f"/{m}/stats"), OIL)
        card["title"] = f"📒 Oil scorecard · model {m}"
        await send(interaction, card)

    @group.command(name="info", description="Oil news the desk is watching: releases ahead, shocks, contract month")
    async def info(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, info_card(await bot.api.oil("/info")))

    bot.tree.add_command(group)
    return group
