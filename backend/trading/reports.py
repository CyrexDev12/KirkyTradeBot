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
    "INSUFFICIENT_EXECUTION_HISTORY": "insufficient market-path history to estimate scalp outcomes",
    "MC_UNAVAILABLE": "Monte Carlo forecast unavailable",
    "GB_UNAVAILABLE": "gradient-boosting forecast unavailable",
    "UNSUPPORTED_UNCERTAINTY": "no documented model-uncertainty treatment for this market",
    "UNSUPPORTED_SETTLEMENT": "settlement rule not supported",
    "STALE_MARKET_DATA": "market data not fresh enough",
    "UNKNOWN_INCREMENTS": "venue tick/quantity increments not verified",
    "NO_LISTED_MARKET": "no verified listed contract",
    "NO_MODEL_CONSENSUS": "models do not both support the contract after costs",
    "NEGATIVE_NET_EXPECTANCY": "insufficient net edge after costs",
    "INSUFFICIENT_LIQUIDITY": "inadequate liquidity",
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


def money(value) -> str:
    v = D(value)
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def cents_text(value) -> str:
    return f"{D(value) * 100:.1f}".rstrip("0").rstrip(".") + "¢"


def no_trade_text(primary: Counter | dict) -> str:
    top = next(iter(primary), None) if primary else None
    if top == "INSUFFICIENT_EXECUTION_HISTORY":
        return "No rated trades this week — insufficient market-path history to estimate scalp outcomes."
    if top:
        return f"No rated trades this week — could not fully evaluate: {CODE_TEXT.get(top, top)}."
    return "No rated trades this week."


def plan_publication(key: str, summary: dict, payload: dict, cfg) -> dict:
    lines = [summary["headline"],
             f"{summary['qualifying_trades']} qualifying trades; {money(summary['equity'])} equity; "
             f"{money(summary['available_cash'])} available cash."]
    reasons = [CODE_TEXT.get(c, c) for c in summary["primary_reasons"]][:3]
    if reasons and not summary["qualifying_trades"]:
        lines.append("Main reasons: " + "; ".join(reasons) + ".")
    if summary["could_not_evaluate"]:
        lines.append(f"{summary['could_not_evaluate']} candidate contracts could not be evaluated "
                     f"(missing evidence), {summary['evaluated_unattractive']} were evaluated and rejected.")
    for t in payload["trades"]:
        lines.append(f"• `{t['trade_id']}` {t['selected_outcome']} {t['market_type']} — entry {cents_text(t['entry_limit'])}, "
                     f"target {cents_text(t['target_limit'])}, stop {cents_text(t['stop_trigger'])}, "
                     f"qty {t['quantity']}")
    return {"publication_id": f"plan:{key}", "kind": "weekly_plan", "channel": "picks", "revision": 1,
            "payload": {"title": f"NFL weekly paper portfolio · {payload['season']} week {payload['week']}",
                        "description": "\n".join(lines),
                        "footer": f"PAPER TRADING · {key} · config {cfg.version}"}}


def trade_card(plan, status: str, reason: str | None = None) -> dict:
    """Structured fields for a trade card (shared by /trade and channel posts)."""
    p = plan.to_dict() if hasattr(plan, "to_dict") else plan
    meta = p.get("meta") or {}
    gb, mc = meta.get("gb") or {}, meta.get("mc") or {}
    est = meta.get("path_estimate") or {}
    rating = meta.get("rating") or {}
    market = p["market_type"] + (f" {p['total_line']}" if p.get("total_line") else "")
    owned = status in ("OPEN", "PARTIALLY_FILLED", "FILLED", "EXIT_WORKING", "PARTIALLY_CLOSED",
                       "SETTLEMENT_PENDING")
    fields = [
        ("Contract", f"{p['game_id']} · {market} · **{p['selected_outcome']}** ({p['selected_side']} side)\n"
                     f"Kickoff {ts(p['kickoff'])}"),
        ("Plan", f"Entry limit **{cents_text(p['entry_limit'])}** · target **{cents_text(p['target_limit'])}** · "
                 f"stop trigger **{cents_text(p['stop_trigger'])}**\nEntry expires {ts(p['entry_expires_at'])} · "
                 f"time exit {ts(p['exit_deadline'])}\nHolding policy {p['hold_policy']}"),
        ("Size and risk", f"Quantity {p['quantity']} · est. all-in cash {money(p['estimated_entry_cost'])}\n"
                          f"Stressed stop loss {money(p['planned_loss'])} · full funded loss "
                          f"{money(p['full_funded_loss'])}"),
        ("Pregame model values (separate)",
         f"GB {gb.get('fair_value')} [{gb.get('lower')}–{gb.get('upper')}]\n"
         f"MC {mc.get('fair_value')} [{mc.get('lower')}–{mc.get('upper')}]"),
    ]
    if est.get("status") == "supported":
        probs = est.get("state_probabilities") or {}
        fields.append(("Path estimate (conditional on fill)",
                       f"P(fill) {est.get('fill_probability')} · target {probs.get('TARGET')} · stop {probs.get('STOP')}"
                       f" · time {probs.get('TIME')}\nLower-bound net return per committed $ {est.get('lower_return')}"
                       f" · {est.get('games')} games / {est.get('weeks')} weeks"))
    else:
        fields.append(("Path estimate", "Unavailable — not shown"))
    comps = rating.get("components") or {}
    fields.append(("Rating", f"**{rating.get('value')}/10** ({rating.get('version')}) — not a probability of profit\n"
                             + " · ".join(f"{k} {v}" for k, v in comps.items())))
    title_status = status if not owned else f"{status} position"
    if status in ("PROPOSED", "WORKING"):
        title_status = f"{status} ENTRY (not a position)"
    return {"title": f"PAPER TRADE | {p['trade_id']} | {title_status}", "status": status, "reason": reason,
            "revision": p.get("revision", 1), "fields": fields,
            "footer": f"PAPER TRADE · {p['trade_id']} · {p['strategy_id']} · quotes as of {meta.get('quote_as_of')}"}


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
            text = f"`{tid}` paper entry working (cash reserved {money(e['reserve'])}; not a position yet)."
        elif t == "ENTRY_FILL":
            text = f"`{tid}` paper entry filled {e['quantity']} @ {cents_text(e['price'])} (fee {money(e['fee'])})."
        elif t == "ENTRY_CLOSED":
            text = f"`{tid}` remaining entry {e['status'].lower()}: {e['reason']}. Reservation released."
        elif t == "EXIT_ACTIVATED" and e["mode"] != "TARGET":
            text = f"`{tid}` {e['mode'].lower()} exit activated."
        elif t == "EXIT_FILL":
            text = (f"`{tid}` {e['reason'].lower()} exit sold {e['quantity']} for {money(e['net_proceeds'])} net "
                    f"(realized {money(e['realized'])}).")
        elif t == "SETTLED":
            text = f"`{tid}` settled {e['quantity']} @ {money(e['payout'])} (realized {money(e['realized'])})."
        elif t == "SETTLEMENT_PENDING":
            text = f"`{tid}` market resolved; settlement pending verification."
        elif t == "OUTAGE":
            text = f"Market data gap on {e['slug']} ({e['seconds']:.0f}s). No fills were simulated in the gap."
        if text:
            out.append({"publication_id": f"alert:{e['event_id']}", "kind": "trade_alert", "channel": "updates",
                        "revision": 1, "payload": {"title": "Paper trade update", "description": text,
                                                   "footer": "PAPER TRADING"}})
    return out


def weekly_report(account: dict, start_equity: Decimal, season: int, week: int, events: list[dict], cfg,
                  data_quality: str | None = None) -> dict:
    end = D(account["equity"])
    change = end - start_equity
    ret = change / start_equity if start_equity else None
    exits = Counter()
    fees = ZERO
    worst = ZERO
    for e in events:
        if e["type"] in ("EXIT_FILL", "SETTLED"):
            exits[e.get("reason", "SETTLEMENT")] += 1
            worst = min(worst, D(e["realized"]))
        if e["type"] in ("ENTRY_FILL", "EXIT_FILL"):
            fees += D(e["fee"])
    entries = Counter(e["status"] for e in events if e["type"] == "ENTRY_CLOSED")
    filled = len({e["trade_id"] for e in events if e["type"] == "ENTRY_FILL"})
    lines = [
        f"Equity {money(start_equity)} → **{money(end)}** ({money(change)}"
        + (f", {ret:+.2%})" if ret is not None else ")"),
        f"Cash {money(account['cash'])} · reserved {money(account['reserved'])} · available "
        f"{money(account['available_cash'])} · open liquidation value {money(account['open_liquidation_value'])}",
        f"Realized {money(account['realized_pnl'])} · unrealized {money(account['unrealized_pnl'])} · "
        f"since inception {money(end - D(account['starting_equity']))}",
        f"Entries filled {filled} · expired {entries.get('EXPIRED', 0)} · canceled {entries.get('CANCELED', 0)}",
        "Exits " + (", ".join(f"{k.lower()} {v}" for k, v in sorted(exits.items())) or "none")
        + f" · fees {money(fees)} · largest loss {money(worst)}",
        f"Open positions {len(account['positions'])}",
    ]
    if account.get("provisional"):
        lines.append("Some marks are stale or incomplete — this report is provisional.")
    if data_quality:
        lines.append(data_quality)
    return {"publication_id": f"report:{cfg.strategy_id}:{season}:{week:02d}", "kind": "weekly_report",
            "channel": "reports", "revision": 1,
            "payload": {"title": f"NFL paper account · {season} week {week}", "description": "\n".join(lines),
                        "footer": f"PAPER TRADING · config {cfg.version}"}}


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
