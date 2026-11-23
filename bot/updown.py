"""Discord cards and delivery for the Polymarket Up/Down paper desks.

Two desks share this code:

* **Crypto Up/Down** (BTC/ETH/SOL 15m) posts to the crypto channel; commands
  ``/updown-account``, ``/updown-trades``, ``/updown-stats``.
* **Gold** (5m/15m/1h) posts to its own ``gold`` channel (``/paper-channels set gold``);
  commands ``/gold-picks``, ``/gold-results``, ``/gold-stats``, ``/gold-account``.

Each bet produces a **pick** card when entered and one result card when it ends:
**win** (settled on the right side), **loss** (settled wrong), **out** (sold early
above fair value), **take profit** (the ticket's target was hit) or **stopped** (the
ticket's stop-loss was hit). Skips are not posted; the latest reasons are in the account card.
"""
from dataclasses import dataclass
from datetime import datetime
import logging

import discord
from discord import app_commands

from .paper import _money, publication_embed

logger = logging.getLogger(__name__)

SIDE = {"UP": "📈 Up", "DOWN": "📉 Down"}
HORIZON = {"5m": "5-min", "15m": "15-min", "1h": "hourly", "1d": "daily"}
NAMES = {"gold": "Gold", "wti": "WTI", "natgas": "Nat gas"}
DECIMALS = {"natgas": 4}  # settlement prices: natural gas is quoted to 4 decimals


def _price(t, value):
    return f"${value or 0:,.{DECIMALS.get(t.get('asset'), 2)}f}"


@dataclass(frozen=True)
class Desk:
    name: str  # "Up/Down" | "Gold"
    emoji: str
    footer: str
    api: str  # bot.api method name
    route_kind: str  # paper_channels kind the cards post to
    cursor_table: str


CRYPTO = Desk("Up/Down", "🎲", "🥩 Beef Kirkey · Polymarket Up/Down paper trading", "updown", "crypto", "updown_cursors")
GOLD = Desk("Gold", "🥇", "🥩 Beef Kirkey · Polymarket gold paper trading", "gold", "gold", "gold_cursors")


def _time(value):
    return f"<t:{int(datetime.fromisoformat(value).timestamp())}:t>" if value else "—"


def _cents(p):
    return f"{p * 100:.0f}¢"


def market_name(t):
    name = NAMES.get(t["asset"], t["asset"].upper())
    return f"{name} {HORIZON.get(t.get('horizon', '15m'), t.get('horizon', ''))}"


def risk_text(t):
    return (f"Stake **{_money(t['cost'])}** = most you can lose ({t.get('max_loss_pct', 0):.1%} of balance). "
            f"Pays {_money(t['payout_if_right'])} if right → **{_money(t['profit_if_right'])}** profit.\n"
            f"Limits: {t.get('max_stake_pct', 0.05):.0%} max per bet · ¼-Kelly sizing · "
            f"no new bets after a {t.get('daily_loss_limit', 0.10):.0%} daily loss.")


def _signed(x):
    return ("+" if x > 0 else "") + _money(x)


def bracket_text(t):
    """The ticket's take-profit / stop-loss lines, or None when the desk doesn't use them."""
    tp, sl = t.get("take_profit_price"), t.get("stop_loss_price")
    if tp is None and sl is None:
        return None
    lines = []
    if tp is not None:
        lines.append(f"🎯 **Take profit:** sell at **{_cents(tp)}** → {_signed(t['profit_at_take_profit'])}")
    if sl is not None:
        lines.append(f"🛑 **Stop loss:** sell at **{_cents(sl)}** → {_signed(t['loss_at_stop'])}")
    if t.get("reward_to_risk") is not None:
        lines.append(f"Reward-to-risk at the bracket **{t['reward_to_risk']:.2f} : 1**")
    lines.append("Otherwise held to settlement. A stop sells into the bids that are there, so a fast move "
                 "can fill below the stop; the most a bet can lose is still its stake.")
    return "\n".join(lines)


def model_tag(t):
    return f" · model {t['model']}" if t.get("model") else ""


def result_label(t):
    if t.get("exit_reason") == "take_profit":
        return "💸 OUT", "sold early above fair value"
    if t.get("exit_reason") == "pre_news":
        return "📰 OUT BEFORE NEWS", "sold ahead of a high-impact release, at or above the entry price"
    if t.get("exit_reason") == "target":
        leg = (t.get("exits") or [{}])[-1]
        return "🎯 TAKE PROFIT", f"take-profit hit · sold at {_cents(leg.get('price', 0))}"
    if t.get("exit_reason") == "stop_loss":
        leg = (t.get("exits") or [{}])[-1]
        return "🛑 STOPPED", f"stop-loss hit · sold at {_cents(leg.get('price', 0))}"
    if t.get("outcome") == t.get("side"):
        return "✅ WIN", f"settled **{t.get('outcome')}** at {_price(t, t.get('settlement_price'))}"
    return "❌ LOSS", f"settled **{t.get('outcome')}** at {_price(t, t.get('settlement_price'))}"


def updown_trade_card(t, desk: Desk = CRYPTO):
    side = SIDE.get(t["side"], t["side"])
    window = (f"{market_name(t)} window {_time(t['window_start'])}–{_time(t['window_end'])} · "
              f"price to beat {_price(t, t['price_to_beat'])}")
    if t.get("status") == "CLOSED":
        label, how = result_label(t)
        if t.get("exits") and t["exits"][-1]["kind"] == "settlement" and len(t["exits"]) > 1:
            how += f" (part sold early first)"
        card = {"title": f"{label} · {market_name(t)} {side} · {_money(t['net_pnl'])}{model_tag(t)}",
                "description": f"{window}\n{how} · balance {_money(t.get('balance_after', 0))}",
                "fields": [("Bet", f"{t['quantity']} × {_cents(t['entry_price'])} · stake {_money(t['cost'])} · "
                                   f"back {_money(t.get('proceeds', 0))}")], "footer": desk.footer}
        rec = t.get("record")
        if rec:
            card["fields"].append(("📒 Running record", f"{rec['wins']}W / {rec['losses']}L"))
        return card
    cross = (f"Coinbase {t.get('coinbase_product') or ''} {_money(t['coinbase_price'])}"
             if t.get("coinbase_price") else "Coinbase —")
    return {"title": f"{desk.emoji} PICK · {market_name(t)} {side} (paper){model_tag(t)}",
            "description": window, "footer": desk.footer, "fields": [
                ("Entry", f"{t['quantity']} contracts at **{_cents(t['entry_price'])}** + {_money(t['entry_fee'])} fee"),
                ("Model vs market", f"Model gives {t['side'].title()} **{t['fair_side']:.0%}** · market charged "
                                    f"{_cents(t['entry_price'])} · edge {t['edge'] * 100:.1f}¢ per contract after fees"),
                ("⚠️ Risk", risk_text(t)),
                *([("🎯 Take profit / 🛑 Stop loss", bracket_text(t))] if bracket_text(t) else []),
                ("Prices used", f"Settlement index {_price(t, t.get('index_price'))} · {cross} · "
                                f"{t.get('seconds_left', 0) / 60:.1f} min left")]}


def updown_account_card(a, desk: Desk = CRYPTO):
    s = a.get("risk_settings") or {}
    card = {"title": f"{desk.emoji} {desk.name} paper account", "footer": desk.footer,
            "description": f"Balance **{_money(a['balance'])}** · {_money(a['realized_pnl'])} all-time · "
                           f"{_money(a['today_pnl'])} today · {_money(a['open_stake'])} at risk now", "fields": []}
    for t in a.get("open_trades", []):
        card["fields"].append((f"Open · {market_name(t)} {SIDE.get(t['side'])}",
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


def updown_stats_card(s, desk: Desk = CRYPTO):
    lines = [f"{s['wins']}W / {s['losses']}L · **{_money(s['net_pnl'])}** on {_money(s['staked'])} staked"]
    if s.get("return_on_stake") is not None:
        lines.append(f"Return on stake {s['return_on_stake']:.1%}")
    fields = [(k.upper(), f"{v['trades']} bets · {v.get('wins', 0)} won · {_money(v['net_pnl'])}")
              for k, v in s["by_asset"].items()]
    if s.get("model_brier") is not None:
        fields.append(("Calibration (lower is better; coin flip = 0.25)",
                       f"Model {s['model_brier']:.3f} vs market mid {s['market_mid_brier']:.3f} "
                       f"over {s['observations']} minute samples in {s['windows_resolved']} windows"))
    return {"title": f"📒 Kirkey’s {desk.name} scorecard", "description": "\n".join(lines), "fields": fields,
            "footer": desk.footer}


def trade_row(t):
    row = f"{market_name(t)} {SIDE.get(t['side'])} {_time(t['window_start'])} · {t['quantity']} @ {_cents(t['entry_price'])}"
    if t.get("status") == "CLOSED":
        return f"{result_label(t)[0]} {row} · {_money(t['net_pnl'])}"
    return f"🎯 {row} · open, stake {_money(t['cost'])}"


class UpdownPublisher:
    def __init__(self, api, posts, publisher, desk: Desk = CRYPTO):
        self.api, self.posts, self.publisher, self.desk = api, posts, publisher, desk

    async def tick(self):
        fetch = getattr(self.api, self.desk.api)
        for route in self.posts.desk_routes(self.desk.route_kind, self.desk.cursor_table):
            try:
                if route["event_id"] is None:  # new subscription: start with the next event
                    account = await fetch("/account")
                    self.posts.advance_desk(self.desk.cursor_table, route["guild_id"], account["latest_id"])
                    continue
                data = await fetch("/events", after=route["event_id"], limit=25)
                for event in data["events"]:
                    trade = event["trade"]
                    pid = f"{self.desk.api}:{trade['id']}:{event['kind']}"
                    await self.publisher.deliver(route, {"publication_id": pid, "revision": 1,
                                                         "payload": updown_trade_card(trade, self.desk)})
                    self.posts.advance_desk(self.desk.cursor_table, route["guild_id"], event["id"])
            except Exception as exc:
                logger.warning("%s publication for guild %s failed (%s)", self.desk.name, route["guild_id"],
                               type(exc).__name__)


def _register(bot, desk: Desk, names: dict):
    fetch = lambda path, **kw: getattr(bot.api, desk.api)(path, **kw)  # noqa: E731

    async def send(interaction, card):
        await interaction.followup.send(embed=publication_embed(card), ephemeral=True)

    @bot.tree.command(name=names["account"], description=f"{desk.name} paper balance, open bets and risk limits")
    @app_commands.guild_only()
    async def account(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, updown_account_card(await fetch("/account"), desk))

    @bot.tree.command(name=names["stats"], description=f"{desk.name} paper results and model calibration vs the market")
    @app_commands.guild_only()
    async def stats(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send(interaction, updown_stats_card(await fetch("/stats"), desk))

    @bot.tree.command(name=names["trades"], description=f"Recent {desk.name} paper bets: wins, losses and outs")
    @app_commands.guild_only()
    async def trades(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = [trade_row(t) for t in (await fetch("/trades", limit=10))["trades"]]
        await send(interaction, {"title": f"{desk.emoji} Last 10 {desk.name} bets", "footer": desk.footer,
                                 "description": "\n".join(rows) or f"No {desk.name} paper bets yet."})

    if "picks" in names:
        @bot.tree.command(name=names["picks"], description=f"Open {desk.name} paper picks with their risk")
        @app_commands.guild_only()
        async def picks(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            data = await fetch("/account")
            fields = [(f"{market_name(t)} {SIDE.get(t['side'])} · ends {_time(t['window_end'])}",
                       f"{t['remaining']} @ {_cents(t['entry_price'])} · model {t['fair_side']:.0%} · "
                       f"stake {_money(t['cost'])} · {_money(t['profit_if_right'])} if right")
                      for t in data.get("open_trades", [])]
            latest = data.get("latest_decisions") or []
            desc = ("No open picks right now." if not fields else f"{len(fields)} open pick(s)")
            if not fields and latest:
                desc += "\n" + "\n".join(f"{d['asset'].upper()} · {d['reason']}" for d in latest)
            await send(interaction, {"title": f"{desk.emoji} {desk.name} picks", "description": desc,
                                     "fields": fields, "footer": desk.footer})


def register_updown_commands(bot):
    _register(bot, CRYPTO, {"account": "updown-account", "stats": "updown-stats", "trades": "updown-trades"})


def register_gold_commands(bot):
    _register(bot, GOLD, {"account": "gold-account", "stats": "gold-stats", "trades": "gold-results",
                          "picks": "gold-picks"})
