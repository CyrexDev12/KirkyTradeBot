"""Discord views and delivery for the dedicated BTC paper account."""
from datetime import datetime
import logging

import discord
from discord import app_commands

from .paper import _money, publication_embed

logger = logging.getLogger(__name__)

FOOTER = "🥩 Beef Kirkey · BTC paper trading"
EXIT_LABEL = {"window_end": "15 minutes up", "stop_loss": "stop hit", "take_profit": "target hit"}
REASONS = {
    "5-minute confirmation or 15-minute trend too weak": "The charts didn’t agree strongly enough on up or down.",
    "15-minute trend confirmed by 5-minute momentum": "The 5-minute and 15-minute charts both leaned this way.",
    "15-minute volatility outside strategy range": "BTC’s price swings fell outside the trading range.",
    "fee-adjusted target exceeds three recent 15-minute average ranges":
        "Even a break-even-plus target needed too big a move after fees for 15 minutes.",
    "bid/ask spread too wide": "The gap between buy and sell prices was too wide.",
    "daily realized loss limit": "The paper account hit its loss limit for today.",
    "entry window expired while retrieving data": "The quote arrived after the entry window.",
    "entry window missed": "The entry window had already closed.",
    "risk budget too small": "The allowed position was smaller than the minimum trade.",
}


def decision_reason(trade):
    reason = trade.get("reason") or "No clear signal."
    if reason.startswith(("HTTPStatusError:", "ConnectError:", "ReadTimeout:", "DataUnavailable:", "ValueError:")):
        return "A fresh Coinbase price or complete candle history wasn’t available in time."
    return REASONS.get(reason, reason)


def direction(trade):
    return "📈 Up" if trade["direction"] == "UP" else "📉 Down"


def record_text(record):
    rate = record["win_rate"]
    ratio = f"**{rate:.0%}** wins" if rate is not None else "Win rate pending"
    return f"{ratio} · {record['wins']}W / {record['losses']}L / {record['flats']} flat"


def _time(value):
    return f"<t:{int(datetime.fromisoformat(value).timestamp())}:f>" if value else "—"


def _planned_outcomes(trade):
    lines = []
    if trade.get("risk_at_stop") is not None:
        lines.append(f"Loss at stop {_money(trade['risk_at_stop'])}")
    if trade.get("target_net_profit") is not None:
        lines.append(f"Profit at target {_money(trade['target_net_profit'])}")
    return " · ".join(lines)


def risk_text(trade):
    """Plain statement of what this paper trade risks, from the stored entry fields."""
    if trade.get("risk_at_stop") is None:
        return None
    parts = [f"Up to **{_money(trade['risk_at_stop'])}** lost if the stop is hit"]
    if trade.get("risk_pct") is not None:
        parts[0] += f" ({trade['risk_pct']:.1%} of balance)"
    if trade.get("position_value") is not None:
        parts.append(f"position {_money(trade['position_value'])} ({trade['position_pct']:.0%} of balance)")
    if trade.get("daily_loss_limit") is not None:
        parts.append(f"no new trades after a {trade['daily_loss_limit']:.0%} daily loss")
    text = " · ".join(parts)
    if trade.get("fee_rate") is not None:
        text += (f"\nA price gap can lose more than the stop amount. Assumes {trade['fee_rate']:.2%} fees each way "
                 "(US Coinbase Advanced lists 0.50% maker / 0.90% taker).")
    return text


def crypto_trade_card(trade):
    status = trade["status"]
    card = {"footer": FOOTER, "description": f"15-minute window · {_time(trade['slot_at'])}", "fields": []}
    if status == "SKIPPED":
        card["title"] = "🪑 BTC sat this one out"
        card["description"] += "\n" + decision_reason(trade)
        return card

    side = direction(trade)
    if status == "CLOSED":
        result = float(trade["net_pnl"])
        card["title"] = f"{side} · BTC closed · {_money(result)} after costs"
        exit_note = EXIT_LABEL.get(trade["exit_reason"], trade["exit_reason"].replace("_", " "))
        card["description"] += f"\n{_money(trade['entry_price'])} → **{_money(trade['exit_price'])}** · {exit_note}"
        if trade.get("record"):
            card["fields"].append(("📒 Running record", record_text(trade["record"])))
        return card
    card["title"] = f"{side} · Kirkey opened a BTC paper trade"
    card["fields"] = [
        ("Entry", f"**{_money(trade['entry_price'])}** · {_money(float(trade['quantity']) * float(trade['entry_price']))} position"),
        ("Exit plan", f"🎯 Target {_money(trade['take_profit'])} · 🛑 Stop {_money(trade['stop_loss'])}"),
    ]
    projections = _planned_outcomes(trade)
    if projections:
        card["fields"].append(("Estimated outcomes after costs", projections))
    risk = risk_text(trade)
    if risk:
        card["fields"].append(("⚠️ Risk", risk))
    if trade.get("reason"):
        card["fields"].append(("Why this trade?", decision_reason(trade)))
    learning = trade.get("learning") or {}
    if learning.get("risk_multiplier", 1) < 1:
        card["fields"].append(("Smaller size", f"Risk cut to {learning['risk_multiplier']:.0%} for this pattern "
                               f"after {learning['closed_trades']} closed trades."))
    return card


def crypto_account_card(account):
    card = {"title": "🪙 BTC paper account", "footer": FOOTER,
            "description": f"Balance **{_money(account['balance'])}** · {_money(account['realized_pnl'])} "
                           f"from {account['closed_trades']} closed trades",
            "fields": []}
    position = account.get("open_trade")
    if position:
        card["fields"].append(("Open position", f"{direction(position)} at {_money(position['entry_price'])}\n"
                               f"Take profit {_money(position['take_profit'])} · Stop loss {_money(position['stop_loss'])}"))
        projections = _planned_outcomes(position)
        if projections:
            card["fields"].append(("Estimated outcomes after costs", projections))
        risk = risk_text(position)
        if risk:
            card["fields"].append(("⚠️ Risk", risk))
    else:
        card["description"] += "\nNo BTC position open."
    decision = account.get("latest_decision")
    if decision and decision["status"] == "SKIPPED":
        card["fields"].append(("Last skip", f"{_time(decision['slot_at'])} · {decision_reason(decision)}"))
    health = account.get("worker") or {}
    if health.get("last_error"):
        card["fields"].append(("⏳ Data delayed", f"Trading check last ran {_time(health.get('last_heartbeat'))}."))
    return card


def crypto_stats_card(stats):
    closed = stats["closed_trades"]
    fields = []
    description = (record_text(stats) if closed else "The scorecard starts when the first paper trade closes.")
    description += f"\nAll-time **{_money(stats['net_pnl'])}** after costs · {closed} closed · {stats['skipped']} skipped"
    learning = stats["learning"]
    patterns = []
    for p in stats["patterns"]:
        risk = f" · risk cut to {p['risk_multiplier']:.0%}" if p["risk_multiplier"] < 1 else ""
        patterns.append(f"**{p['label']}** · {p['wins']}/{p['closed_trades']} won · {_money(p['net_pnl'])}{risk}")
    if patterns:
        fields.append((f"🕒 Last {learning['lookback_days']} days · up to {learning['lookback']} trades per group",
                       "\n".join(patterns)))
    fields.append(("What Kirkey learns", f"Tracks up and down trades separately by hour. "
                   f"After {learning['min_trades']} trades, a consistently losing group can get half-size entries."))
    return {"title": "📒 Kirkey’s BTC scorecard", "description": description, "fields": fields, "footer": FOOTER}


class CryptoPublisher:
    def __init__(self, api, posts, publisher):
        self.api, self.posts, self.publisher = api, posts, publisher

    async def tick(self):
        for route in self.posts.crypto_routes():
            try:
                data = await self.api.crypto("/events", after=route["event_id"], limit=25)
                for event in data["events"]:
                    trade = event["trade"]
                    # Stable across a process restart, even if delivery reached Discord before cursor advancement.
                    pid = f"crypto:{trade['slot_at']}:{event['kind']}"
                    await self.publisher.deliver(route, {"publication_id": pid, "revision": 1,
                                                         "payload": crypto_trade_card(trade)})
                    self.posts.advance_crypto(route["guild_id"], event["id"])
            except Exception as exc:
                logger.warning("BTC paper publication for guild %s failed (%s)", route["guild_id"], type(exc).__name__)


def register_crypto_commands(bot):
    @bot.tree.command(name="crypto-account", description="See your BTC paper balance and open position")
    @app_commands.guild_only()
    async def account(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.crypto("/account")
        await interaction.followup.send(embed=publication_embed(crypto_account_card(data)), ephemeral=True)

    @bot.tree.command(name="crypto-stats", description="See Kirkey’s BTC win rate and the times and directions it learns from")
    @app_commands.guild_only()
    async def stats(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.crypto("/stats")
        await interaction.followup.send(embed=publication_embed(crypto_stats_card(data)), ephemeral=True)

    @bot.tree.command(name="crypto-trades", description="See recent BTC paper entries, exits and skips")
    @app_commands.guild_only()
    async def trades(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await bot.api.crypto("/trades", limit=10)
        rows = []
        for trade in data["trades"]:
            desc = f"{_time(trade['slot_at'])} · "
            if trade["status"] == "SKIPPED":
                desc += f"🪑 Skipped · {decision_reason(trade)}"
            else:
                desc += f"{direction(trade)} · {trade['status'].title()} · entry {_money(trade['entry_price'])}"
                if trade["status"] == "CLOSED":
                    desc += f" · {_money(trade['net_pnl'])} net ({trade['exit_reason'].replace('_', ' ')})"
            rows.append(desc)
        card = {"title": "🪙 Last 10 BTC windows", "footer": FOOTER,
                "description": "\n".join(rows) or "The first BTC window hasn’t been recorded yet."}
        await interaction.followup.send(embed=publication_embed(card), ephemeral=True)
