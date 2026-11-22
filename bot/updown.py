"""Discord cards and delivery for the Polymarket Up/Down paper account.

Only entries and closes are posted (skips happen every few seconds and stay in
/updown-account). Cards share the crypto channel set by /paper-channels.
"""
from datetime import datetime
import logging

import discord
from discord import app_commands

from .paper import _money, publication_embed

logger = logging.getLogger(__name__)

FOOTER = "🥩 Beef Kirkey · Polymarket Up/Down paper trading"
SIDE = {"UP": "📈 Up", "DOWN": "📉 Down"}


def _time(value):
    return f"<t:{int(datetime.fromisoformat(value).timestamp())}:t>" if value else "—"


def _cents(p):
    return f"{p * 100:.0f}¢"


def risk_text(t):
    return (f"Stake **{_money(t['cost'])}** = most you can lose ({t.get('max_loss_pct', 0):.1%} of balance). "
            f"Pays {_money(t['payout_if_right'])} if right → **{_money(t['profit_if_right'])}** profit.\n"
            f"Limits: {t.get('max_stake_pct', 0.05):.0%} max per bet · ¼-Kelly sizing · "
            f"no new bets after a {t.get('daily_loss_limit', 0.10):.0%} daily loss.")


def updown_trade_card(t):
    asset, side = t["asset"].upper(), SIDE.get(t["side"], t["side"])
    window = f"{asset} 15-min window {_time(t['window_start'])}–{_time(t['window_end'])} · beat {_money(t['price_to_beat'])}"
    if t.get("status") == "CLOSED":
        how = {"settlement": f"settled **{t.get('outcome')}** at {_money(t.get('settlement_price') or 0)}",
               "take_profit": "sold early above fair value"}.get(t["exit_reason"], t["exit_reason"])
        card = {"title": f"{side} · {asset} Up/Down closed · {_money(t['net_pnl'])}",
                "description": f"{window}\n{how} · balance {_money(t.get('balance_after', 0))}", "fields": [],
                "footer": FOOTER}
        rec = t.get("record")
        if rec:
            card["fields"].append(("📒 Running record", f"{rec['wins']}W / {rec['losses']}L"))
        return card
    return {"title": f"{side} · Kirkey bought {asset} {t['side'].title()} on Polymarket (paper)",
            "description": window, "footer": FOOTER, "fields": [
                ("Entry", f"{t['quantity']} contracts at **{_cents(t['entry_price'])}** + {_money(t['entry_fee'])} fee"),
                ("Model vs market", f"Model gives {t['side'].title()} **{t['fair_side']:.0%}** · market charged "
                                    f"{_cents(t['entry_price'])} · edge {t['edge'] * 100:.1f}¢ per contract after fees"),
                ("⚠️ Risk", risk_text(t)),
                ("Prices used", f"Settlement index {_money(t.get('index_price') or 0)} · Coinbase "
                                f"{_money(t['coinbase_price']) if t.get('coinbase_price') else '—'} · "
                                f"{t.get('seconds_left', 0) / 60:.1f} min left")]}


def updown_account_card(a):
    s = a.get("risk_settings") or {}
    card = {"title": "🎲 Up/Down paper account", "footer": FOOTER,
            "description": f"Balance **{_money(a['balance'])}** · {_money(a['realized_pnl'])} all-time · "
                           f"{_money(a['today_pnl'])} today · {_money(a['open_stake'])} at risk now", "fields": []}
    for t in a.get("open_trades", []):
        card["fields"].append((f"Open · {t['asset'].upper()} {SIDE.get(t['side'])}",
                               f"{t['remaining']} contracts · stake {_money(t['cost'])} · ends {_time(t['window_end'])}"))
    if s:
        card["fields"].append(("⚠️ Risk settings", f"Bet when edge ≥ {s['min_edge'] * 100:.0f}¢ after fees, "
                               f"prices {s['price_range'][0] * 100:.0f}–{s['price_range'][1] * 100:.0f}¢ · "
                               f"max {s['max_stake_pct']:.0%} per bet, {s['max_open_stake_pct']:.0%} open · "
                               f"daily stop {s['daily_loss_limit']:.0%}"))
    latest = a.get("latest_decisions") or []
    if latest:
        card["fields"].append(("Latest checks", "\n".join(f"{d['asset'].upper()} · {d['reason']}" for d in latest)))
    return card


def updown_stats_card(s):
    lines = [f"{s['wins']}W / {s['losses']}L · **{_money(s['net_pnl'])}** on {_money(s['staked'])} staked"]
    if s.get("return_on_stake") is not None:
        lines.append(f"Return on stake {s['return_on_stake']:.1%}")
    fields = [(a.upper(), f"{v['trades']} trades · {_money(v['net_pnl'])}") for a, v in s["by_asset"].items()]
    if s.get("model_brier") is not None:
        fields.append(("Calibration (lower is better; coin flip = 0.25)",
                       f"Model {s['model_brier']:.3f} vs market mid {s['market_mid_brier']:.3f} "
                       f"over {s['observations']} minute samples in {s['windows_resolved']} windows"))
    return {"title": "📒 Kirkey’s Up/Down scorecard", "description": "\n".join(lines), "fields": fields, "footer": FOOTER}


class UpdownPublisher:
    def __init__(self, api, posts, publisher):
        self.api, self.posts, self.publisher = api, posts, publisher

    async def tick(self):
        for route in self.posts.updown_routes():
            try:
                if route["event_id"] is None:  # new subscription: start with the next event
                    account = await self.api.updown("/account")
                    self.posts.advance_updown(route["guild_id"], account["latest_id"])
                    continue
                data = await self.api.updown("/events", after=route["event_id"], limit=25)
                for event in data["events"]:
                    trade = event["trade"]
                    pid = f"updown:{trade['id']}:{event['kind']}"
                    await self.publisher.deliver(route, {"publication_id": pid, "revision": 1,
                                                         "payload": updown_trade_card(trade)})
                    self.posts.advance_updown(route["guild_id"], event["id"])
            except Exception as exc:
                logger.warning("Up/Down publication for guild %s failed (%s)", route["guild_id"], type(exc).__name__)


def register_updown_commands(bot):
    async def send(interaction, card):
        await interaction.followup.send(embed=publication_embed(card), ephemeral=True)

    @bot.tree.command(name="updown-account", description="Polymarket Up/Down paper balance, open bets and risk limits")
    @app_commands.guild_only()
    async def account(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, updown_account_card(await bot.api.updown("/account")))

    @bot.tree.command(name="updown-stats", description="Up/Down paper results and model calibration vs the market")
    @app_commands.guild_only()
    async def stats(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, updown_stats_card(await bot.api.updown("/stats")))

    @bot.tree.command(name="updown-trades", description="Recent Polymarket Up/Down paper bets")
    @app_commands.guild_only()
    async def trades(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = []
        for t in (await bot.api.updown("/trades", limit=10))["trades"]:
            row = f"{t['asset'].upper()} {SIDE.get(t['side'])} {_time(t['window_start'])} · {t['quantity']} @ {_cents(t['entry_price'])}"
            row += f" · {_money(t['net_pnl'])}" if t.get("status") == "CLOSED" else " · open"
            rows.append(row)
        await send(interaction, {"title": "🎲 Last 10 Up/Down bets", "footer": FOOTER,
                                 "description": "\n".join(rows) or "No Up/Down paper bets yet."})
