"""Deterministic trade cards, weekly plans and account reports from stored fields.

No free-form model text: every number shown comes from a stored plan, decision,
journal event or account calculation. A working entry is never described as an
owned position.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from .common import ZERO, D, utc

CODE_TEXT = {
    "INSUFFICIENT_EXECUTION_HISTORY": "we need more price history to judge when a trade could exit",
    "MC_UNAVAILABLE": "Monte Carlo forecast unavailable",
    "GB_UNAVAILABLE": "gradient-boosting forecast unavailable",
    "UNSUPPORTED_UNCERTAINTY": "the models don’t have a reliable range for this market yet",
    "UNSUPPORTED_SETTLEMENT": "settlement rule not supported",
    "STALE_MARKET_DATA": "market data not fresh enough",
    "UNKNOWN_INCREMENTS": "we couldn’t verify the permitted order size or price",
    "NO_LISTED_MARKET": "we couldn’t find a matching market",
    "NO_MODEL_CONSENSUS": "the two models didn’t agree on a pick after costs",
    "NEGATIVE_NET_EXPECTANCY": "the price left too little upside after costs",
    "INSUFFICIENT_LIQUIDITY": "too few shares were available at the quoted price",
    "PORTFOLIO_LIMIT": "portfolio limits",
    "RATING_BELOW_MINIMUM": "rating below 6.5",
    "RATING_UNAVAILABLE": "rating evidence incomplete",
    "MARKET_NOT_OPEN": "market not open",
    "ENTRY_WINDOW_CLOSED": "entry window closed",
    "ENTRIES_SUSPENDED": "new entries suspended",
    "PLAN_INVALID_FOR_BOOK": "no legal target/stop for the current book",
    "CONTRACT_MAPPING_FAILED": "contract mapping failed verification",
}


def ts(value) -> str:
    if value is None:
        return "—"
    return f"<t:{int(utc(value).timestamp())}:f>"


def _c(value) -> str:
    return cents_text(value) if value is not None else "—"


def money(value) -> str:
    v = D(value)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def cents_text(value) -> str:
    return f"{D(value) * 100:.1f}".rstrip("0").rstrip(".") + "¢"


def matchup_name(game_id: str) -> str:
    parts = game_id.split("_")
    return f"{parts[2]} at {parts[3]}" if len(parts) == 4 else game_id


def no_trade_text(primary: Counter | dict) -> str:
    top = next(iter(primary), None) if primary else None
    if top:
        return f"Kirkey’s sitting out this week — {CODE_TEXT.get(top, top.lower().replace('_', ' '))}."
    return "Kirkey’s sitting out this week."


def plan_publication(key: str, summary: dict, payload: dict, cfg) -> dict:
    lines = [summary["headline"], f"Paper balance **{money(summary['equity'])}** · "
             f"{money(summary['available_cash'])} available"]
    for t in payload["trades"]:
        line = f" {t['total_line']}" if t.get("total_line") is not None else ""
        lines.append(f"• {matchup_name(t['game_id'])} · **{t['selected_outcome']}{line}** · {cents_text(t['entry_limit'])} entry · "
                     f"{cents_text(t['target_limit'])} target · {cents_text(t['stop_trigger'])} stop · `{t['trade_id']}`")
    return {"publication_id": f"plan:{key}", "kind": "weekly_plan", "channel": "picks", "revision": 1,
            "payload": {"title": f"🏈 Kirkey’s paper picks · Week {payload['week']}, {payload['season']}",
                        "description": "\n".join(lines),
                        "footer": "Beef Kirkey · NFL paper trading"}}


def trade_card(plan, status: str, reason: str | None = None) -> dict:
    """Structured fields for a trade card (shared by /trade and channel posts)."""
    p = plan.to_dict() if hasattr(plan, "to_dict") else plan
    meta = p.get("meta") or {}
    gb, mc = meta.get("gb") or {}, meta.get("mc") or {}
    rating = meta.get("rating") or {}
    market = p["market_type"] + (f" {p['total_line']}" if p.get("total_line") else "")
    label = {"PROPOSED": "Entry planned", "WORKING": "Waiting to buy", "EXIT_WORKING": "Waiting to sell",
             "PARTIALLY_FILLED": "Partly filled", "PARTIALLY_CLOSED": "Partly sold"}.get(
                 status, status.replace("_", " ").title())
    fields = [
        ("Game", f"{matchup_name(p['game_id'])} · kickoff {ts(p['kickoff'])}"),
        ("Price plan", f"Buy up to **{cents_text(p['entry_limit'])}** · 🎯 {cents_text(p['target_limit'])} target · "
                       f"🛑 {cents_text(p['stop_trigger'])} stop\nEntry expires {ts(p['entry_expires_at'])} · "
                       f"exit deadline {ts(p['exit_deadline'])}"),
        ("Paper stake", f"{p['quantity']} shares · estimated cost **{money(p['estimated_entry_cost'])}**\n"
                        f"Estimated loss at stop {money(p['planned_loss'])} · maximum loss {money(p['full_funded_loss'])}"),
        ("Model prices", f"Gradient {_c(gb.get('fair_value'))} · Monte Carlo {_c(mc.get('fair_value'))}"),
    ]
    if rating.get("value") is not None:
        fields.append(("Kirkey’s grade", f"**{rating['value']}/10**"))
    return {"title": f"🏈 {p['selected_outcome']} · {market} · {label}", "status": status, "reason": reason,
            "revision": p.get("revision", 1), "fields": fields,
            "footer": f"Beef Kirkey · NFL paper trade {p['trade_id']}"}


def trade_card_publication(plan, status, reason, cfg, revision: int = 1) -> dict:
    card = trade_card(plan, status, reason)
    if reason:
        card["fields"].append(("Status note", reason))
    return {"publication_id": f"trade:{plan.trade_id}", "kind": "trade_card", "channel": "picks",
            "revision": revision, "payload": card}


def lifecycle_alerts(events: list[dict]) -> list[dict]:
    """Concise alerts for lifecycle transitions; never one per quote."""
    out = []
    for e in events:
        t, tid = e["type"], e.get("trade_id")
        text = None
        if t == "ENTRY_WORKING":
            text = f"`{tid}` · Waiting to buy. {money(e['reserve'])} set aside."
        elif t == "ENTRY_FILL":
            text = f"`{tid}` · Bought **{e['quantity']} shares at {cents_text(e['price'])}** · fee {money(e['fee'])}."
        elif t == "ENTRY_CLOSED":
            text = f"`{tid}` · Remaining order {e['status'].lower()}. Unused cash is available again."
        elif t == "EXIT_ACTIVATED" and e["mode"] != "TARGET":
            text = f"`{tid}` {e['mode'].lower()} exit activated."
        elif t == "EXIT_FILL":
            text = (f"`{tid}` {e['reason'].lower()} exit sold {e['quantity']} for {money(e['net_proceeds'])} net "
                    f"(realized {money(e['realized'])}).")
        elif t == "SETTLED":
            text = f"`{tid}` settled {e['quantity']} @ {money(e['payout'])} (realized {money(e['realized'])})."
        elif t == "SETTLEMENT_PENDING":
            text = f"`{tid}` · Game over. Waiting to confirm the payout."
        elif t == "OUTAGE":
            text = f"Market data gap on {e['slug']} ({e['seconds']:.0f}s). No fills were simulated in the gap."
        if text:
            out.append({"publication_id": f"alert:{e['event_id']}", "kind": "trade_alert", "channel": "updates",
                        "revision": 1, "payload": {"title": "🏈 NFL trade update", "description": text,
                                                   "footer": "Beef Kirkey · NFL paper trading"}})
    return out


def weekly_report(account: dict, start_equity: Decimal, season: int, week: int, events: list[dict], cfg,
                  data_quality: str | None = None) -> dict:
    end = D(account["equity"])
    change = end - start_equity
    ret = change / start_equity if start_equity else None
    fees = ZERO
    for e in events:
        if e["type"] in ("ENTRY_FILL", "EXIT_FILL"):
            fees += D(e["fee"])
    filled = len({e["trade_id"] for e in events if e["type"] == "ENTRY_FILL"})
    lines = [
        f"Account value {money(start_equity)} → **{money(end)}** ({money(change)}"
        + (f", {ret:+.2%})" if ret is not None else ")"),
        f"{filled} trades entered · {money(fees)} in fees · {len(account['positions'])} positions still open",
        f"Cash available **{money(account['available_cash'])}**",
    ]
    if account.get("provisional"):
        lines.append("⏳ Some prices are delayed. The account value may change.")
    if data_quality:
        lines.append(data_quality)
    return {"publication_id": f"report:{cfg.strategy_id}:{season}:{week:02d}", "kind": "weekly_report",
            "channel": "reports", "revision": 1,
            "payload": {"title": f"📋 Kirkey’s weekly wrap · Week {week}, {season}", "description": "\n".join(lines),
                        "footer": "Beef Kirkey · NFL paper trading"}}


def performance(engine, snapshots: list[dict]) -> dict:
    s = engine.state
    closed = [p for p in s.positions.values() if p.status == "CLOSED"]
    wins = [p.realized for p in closed if p.realized > 0]
    losses = [p.realized for p in closed if p.realized <= 0]
    peak, max_dd = None, ZERO
    for snap in snapshots:
        eq = D(snap["equity"])
        peak = eq if peak is None or eq > peak else peak
        if peak and peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    orders = list(s.orders.values())
    exit_counts = Counter()
    for p in closed:
        reasons = [r for r, q in p.exit_qty.items() if D(q) > 0]
        exit_counts["MIXED" if len(reasons) > 1 else (reasons[0] if reasons else "NONE")] += 1
    return {"closed_trades": len(closed), "wins": len(wins), "losses": len(losses),
            "average_win": str(sum(wins, ZERO) / len(wins)) if wins else None,
            "average_loss": str(sum(losses, ZERO) / len(losses)) if losses else None,
            "expectancy": str(sum((p.realized for p in closed), ZERO) / len(closed)) if closed else None,
            "fees": str(sum((p.entry_fees + p.exit_fees for p in s.positions.values()), ZERO)),
            "entries": len(orders), "entries_any_fill": sum(1 for o in orders if o.filled > 0),
            "entries_full_fill": sum(1 for o in orders if o.filled >= o.quantity),
            "exit_outcomes": dict(exit_counts), "max_drawdown": str(max_dd), "outages": len(s.outages)}
