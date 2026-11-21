"""Measure completed paper trades and reduce exposure in repeatedly weak setups."""
from collections import defaultdict
from datetime import datetime, timedelta
import json
from statistics import mean

from .strategy import PHOENIX


LOOKBACK_DAYS = 90
MAX_TRADES = 40
MIN_TRADES = 20


def record(db):
    row = db.execute("""
        SELECT count(*) AS closed_trades,
               count(CASE WHEN net_pnl > 0 THEN 1 END) AS wins,
               count(CASE WHEN net_pnl < 0 THEN 1 END) AS losses,
               count(CASE WHEN net_pnl = 0 THEN 1 END) AS flats,
               coalesce(sum(net_pnl), 0) AS net_pnl
        FROM decisions WHERE status = 'CLOSED'
    """).fetchone()
    result = dict(row)
    result["net_pnl"] = round(result["net_pnl"], 8)
    result["win_rate"] = result["wins"] / result["closed_trades"] if result["closed_trades"] else None
    return result


def pattern_label(slot, direction):
    hour = slot.astimezone(PHOENIX).hour
    return f"{direction.title()} · {hour % 12 or 12} {'AM' if hour < 12 else 'PM'} Arizona"


def recent_patterns(db, as_of):
    # Older journal versions already retained the side, slot, net P&L and entry risk.
    # A fill more than one minute past expiry after an outage still counts toward
    # the account record, but cannot describe the intended 15-minute opportunity.
    rows = db.execute("""
        SELECT payload, net_pnl FROM decisions
        WHERE status = 'CLOSED'
          AND julianday(slot) BETWEEN julianday(?) AND julianday(?)
          AND julianday(json_extract(payload, '$.closed_at')) <= julianday(?)
          AND julianday(coalesce(json_extract(payload, '$.exit_quote_at'),
                                 json_extract(payload, '$.closed_at'))) <= julianday(slot, '+16 minutes')
        ORDER BY slot DESC
    """, ((as_of - timedelta(days=LOOKBACK_DAYS)).isoformat(), as_of.isoformat(), as_of.isoformat()))
    groups = defaultdict(list)
    for row in rows:
        trade = json.loads(row["payload"])
        key = pattern_label(datetime.fromisoformat(trade["slot_at"]), trade["direction"])
        if len(groups[key]) < MAX_TRADES:
            groups[key].append((row["net_pnl"], trade.get("risk_at_stop")))
    return {label: summarize_pattern(label, observations) for label, observations in groups.items()}


def summarize_pattern(label, observations=()):
    n = len(observations)
    profits = [profit for profit, _ in observations]
    wins = sum(profit > 0 for profit in profits)
    losses = sum(profit < 0 for profit in profits)
    # Each result contributes equally regardless of account size or earlier risk reductions.
    multiples = [profit / risk for profit, risk in observations if risk and risk > 0]
    risk_multiplier = 1.0
    if len(multiples) == n and n >= MIN_TRADES and wins / n < 0.4:
        halfway = n // 2
        if mean(multiples[:halfway]) < 0 and mean(multiples[halfway:]) < 0:
            risk_multiplier = 0.5
    return {"label": label, "closed_trades": n, "wins": wins, "losses": losses, "flats": n - wins - losses,
            "win_rate": wins / n if n else None, "net_pnl": round(sum(profits), 8),
            "avg_r": round(mean(multiples), 4) if multiples else None, "risk_multiplier": risk_multiplier}


def performance(db, now):
    counts = dict(db.execute("SELECT status, count(*) FROM decisions GROUP BY status"))
    patterns = recent_patterns(db, now)
    return {**record(db), "skipped": counts.get("SKIPPED", 0), "open_trades": counts.get("OPEN", 0),
            "patterns": [patterns[label] for label in sorted(patterns)],
            "learning": {"min_trades": MIN_TRADES, "lookback": MAX_TRADES, "lookback_days": LOOKBACK_DAYS,
                         "description": "Half size for a direction and hour with under 40% wins and losses "
                         "on average in both halves of its recent sample. Other setups use normal sizing."}}
