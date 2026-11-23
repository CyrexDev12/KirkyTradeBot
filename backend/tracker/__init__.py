"""Tracker: one read-only view over every paper-trading bot (data feed for Chief Beef Kirkey).

It reads each desk's own journal and health file (it never writes to them) and
returns, per desk, a traffic-light state plus balance, today's P&L, open risk and
record, and a single normalized list of recent trades across all desks:

* ``nfl``     — NFL Polymarket paper account (``backend.trading``)
* ``btc``     — BTC spot paper bot on Coinbase (``backend.crypto``)
* ``updown``  — Polymarket crypto Up/Down 15m (``backend.updown``)
* ``gold``    — Polymarket gold Up/Down 5m/15m/1h (``backend.gold``)
* ``oil-A`` … ``oil-E`` — Polymarket WTI crude Up/Down, one desk per competing model (``backend.wti``)

States: ``ok`` (healthy), ``warn`` (running but erroring, paused, at its daily loss
limit or in a deep drawdown), ``down`` (no heartbeat or a stale one).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
PHOENIX = ZoneInfo("America/Phoenix")
CLOSED_STATUSES = ("WIN", "LOSS", "OUT", "TARGET", "STOP", "FLAT")
STALE_SECONDS = {"nfl": 120, "btc": 180, "updown": 180, "gold": 180, "oil": 180}
DRAWDOWN_WARN = 0.20  # warn when balance is 20% or more below where the desk started


def _age(heartbeat: str | None, now: datetime) -> float | None:
    if not heartbeat:
        return None
    try:
        ts = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds())


def _read_health(path: Path):
    try:
        return json.loads(path.read_text()) if path.is_file() else None
    except (OSError, ValueError):
        return None


def _desk(desk_id, name, emoji, now, health, **fields):
    age = _age((health or {}).get("last_heartbeat"), now)
    notes = list(fields.pop("notes", []))
    if health is None:
        state, notes = "down", ["worker not running (no heartbeat file)"] + notes
    elif age is None or age > STALE_SECONDS.get(desk_id.split("-")[0], 180):
        state = "down"
        notes = [f"no heartbeat for {int(age // 60) if age else '?'} min"] + notes
    else:
        state = "warn" if fields.pop("warn", False) else "ok"
    fields.pop("warn", None)
    start, balance = fields.get("starting_balance"), fields.get("balance")
    if state == "ok" and start and balance is not None and balance <= start * (1 - DRAWDOWN_WARN):
        state = "warn"
        notes.append(f"down {1 - balance / start:.0%} from its starting balance")
    return {"id": desk_id, "name": name, "emoji": emoji, "state": state, "heartbeat_age_s": age,
            "notes": notes, **fields}


# ---------------------------------------------------------------- NFL desk
def _nfl(now, limit=40):
    from ..trading.api import cfg, store
    from ..trading.worker import HEALTH_PATH

    health = _read_health(HEALTH_PATH)
    engine = store().load_engine(cfg())
    acct = engine.account(now)
    s = engine.state
    trades = []
    for tid, pos in s.positions.items():
        plan = s.plans.get(tid)
        if not plan or pos.qty_bought <= 0:
            continue
        closed = pos.status == "CLOSED"
        pnl = float(pos.realized) if closed else None
        trades.append({"desk": "nfl", "id": tid, "market": f"{plan.game_id} · {plan.market_type}",
                       "side": str(plan.selected_outcome).upper(), "stake": float(pos.total_cost),
                       "status": ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT") if closed else "OPEN",
                       "pnl": pnl, "at": (pos.last_entry_fill_at.isoformat() if pos.last_entry_fill_at else None)})
    for order in acct["working_entries"]:
        plan = s.plans.get(order["trade_id"])
        if plan and order["trade_id"] not in s.positions:
            trades.append({"desk": "nfl", "id": order["trade_id"], "market": f"{plan.game_id} · {plan.market_type}",
                           "side": str(plan.selected_outcome).upper(), "stake": float(order["reserved"]),
                           "status": "WORKING", "pnl": None, "at": None})
    notes, warn = [], False
    if acct["paused"]:
        warn = True
        notes.append(f"paused: {acct.get('pause_reason') or 'operator'}")
    if health and health.get("last_error"):
        warn = True
        notes.append(f"last error: {str(health['last_error'])[:120]}")
    if health and health.get("phase"):
        notes.append(f"phase: {health['phase']}")
    start = float(acct["starting_equity"]) if acct.get("starting_equity") else None
    open_positions = [p for p in acct["positions"]]
    return _desk("nfl", "NFL Polymarket", "🏈", now, health, warn=warn, notes=notes,
                 starting_balance=start, balance=float(acct["equity"]),
                 realized_pnl=float(acct["realized_pnl"]), today_pnl=None,
                 open_count=len(open_positions) + len(acct["working_entries"]),
                 at_risk=sum(float(p["cost_basis"]) for p in open_positions) + float(acct["reserved"]),
                 record=_record(trades)), trades


# ---------------------------------------------------------------- BTC spot
def _btc(now, limit=40):
    from ..crypto import strategy as spot
    from ..crypto.api import store
    from ..crypto.store import data_directory

    health = _read_health(data_directory() / "health.json")
    acct = store().account()
    rows = store().trades(limit)
    today_key = now.astimezone(PHOENIX).date().isoformat()
    with store().transaction() as db:
        today = db.execute("SELECT coalesce(sum(net_pnl),0) FROM decisions WHERE local_day=?", (today_key,)).fetchone()[0]
    trades = []
    for t in rows:
        if t.get("status") not in ("OPEN", "CLOSED"):
            continue
        pnl = t.get("net_pnl") if t["status"] == "CLOSED" else None
        trades.append({"desk": "btc", "id": t["slot_at"], "market": "BTC-USD spot", "side": t.get("direction"),
                       "stake": round(t.get("entry_price", 0) * t.get("quantity", 0), 4),
                       "status": ("WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT") if pnl is not None else "OPEN",
                       "pnl": pnl, "exit": t.get("exit_reason"), "at": t.get("closed_at") or t.get("opened_at")})
    limit = spot.SETTINGS.daily_loss_limit
    notes, warn = [], False
    if health and health.get("last_error"):
        warn = True
        notes.append(f"last error: {str(health['last_error'])[:120]}")
    if today <= -limit * (acct["balance"] - today):
        warn = True
        notes.append(f"hit its {limit:.0%} daily loss limit")
    latest = acct.get("latest_decision") or {}
    if latest.get("status") == "SKIPPED" and latest.get("reason"):
        notes.append(f"last window skipped: {latest['reason']}")
    pos = acct.get("open_trade")
    return _desk("btc", "BTC spot (Coinbase)", "₿", now, health, warn=warn, notes=notes,
                 starting_balance=acct["starting_balance"], balance=acct["balance"],
                 realized_pnl=acct["realized_pnl"], today_pnl=round(today, 4), open_count=1 if pos else 0,
                 at_risk=round(pos["risk_at_stop"], 4) if pos and pos.get("risk_at_stop") else 0.0,
                 record=_record(trades)), trades


# ---------------------------------------------------------------- Up/Down desks
def _updown_like(desk_id, name, emoji, store, health_path, settings, now, limit=40, error_prefix=None):
    health = _read_health(health_path)
    if health and error_prefix:  # a shared worker (WTI): keep this model's errors and the desk's own
        health = {**health, "errors": {k: v for k, v in (health.get("errors") or {}).items()
                                       if k.startswith(error_prefix) or ":" not in k}}
    snap = store.snapshot(now.timestamp())
    acct = store.account(now.timestamp())
    trades = []
    for t in store.trades(limit):
        if t.get("status") == "CLOSED":
            status = {"take_profit": "OUT", "pre_news": "OUT", "target": "TARGET", "stop_loss": "STOP"}.get(
                t.get("exit_reason"), "WIN" if t.get("outcome") == t.get("side") else "LOSS")
        else:
            status = "OPEN"
        trades.append({"desk": desk_id, "id": str(t["id"]), "market": t.get("key") or t.get("asset"),
                       "side": t.get("side"), "stake": t.get("cost"), "status": status,
                       "pnl": t.get("net_pnl") if status != "OPEN" else None,
                       "at": t.get("closed_at") or t.get("opened_at")})
    notes, warn = [], False
    errors = {k: v for k, v in ((health or {}).get("errors") or {}).items() if not k.startswith("coinbase")}
    if errors:
        warn = True
        notes.append("errors: " + "; ".join(f"{k}: {v}" for k, v in list(errors.items())[:2])[:160])
    if snap["today_pnl"] <= -settings.daily_loss_limit * (snap["balance"] - snap["today_pnl"]):
        warn = True
        notes.append(f"hit its {settings.daily_loss_limit:.0%} daily loss limit")
    latest = acct.get("latest_decisions") or []
    if latest and not acct["open_trades"]:
        notes.append(f"latest check: {latest[0]['asset']} · {latest[0]['reason']}")
    return _desk(desk_id, name, emoji, now, health, warn=warn, notes=notes,
                 starting_balance=acct["starting_balance"], balance=acct["balance"],
                 realized_pnl=acct["realized_pnl"], today_pnl=acct["today_pnl"],
                 open_count=len(acct["open_trades"]), at_risk=acct["open_stake"], record=_record(trades)), trades


def _updown(now, limit=40):
    from ..updown.api import _store
    from ..updown.store import data_directory
    from ..updown.strategy import SETTINGS

    directory = data_directory()
    return _updown_like("updown", "Crypto Up/Down 15m", "🎲", _store(directory / "paper.sqlite3"),
                        directory / "health.json", SETTINGS, now, limit)


def _gold(now, limit=40):
    from ..gold import SETTINGS, data_directory
    from ..updown.api import _store

    directory = data_directory()
    return _updown_like("gold", "Gold Up/Down", "🥇", _store(directory / "paper.sqlite3"),
                        directory / "health.json", SETTINGS, now, limit)


def _record(trades):
    done = [t for t in trades if t["status"] in CLOSED_STATUSES]
    return {"wins": sum(t["pnl"] > 0 for t in done), "losses": sum(t["pnl"] < 0 for t in done),
            "closed": len(done), "recent_pnl": round(sum(t["pnl"] for t in done), 4)}


def _wti(model_id):
    def collect_model(now, limit=40):
        from ..updown.api import _store
        from ..wti import SETTINGS, data_directory
        from ..wti.models import MODEL_INFO
        directory = data_directory()
        return _updown_like(f"oil-{model_id}", f"Oil · {model_id} {MODEL_INFO[model_id][0]}", "🛢️",
                            _store(directory / "models" / model_id / "paper.sqlite3"), directory / "health.json",
                            SETTINGS, now, limit, error_prefix=f"{model_id}:")
    return collect_model


def _wti_collectors():
    from ..wti import MODELS
    from ..wti.models import MODEL_INFO
    return tuple((f"oil-{m}", f"Oil · {m} {MODEL_INFO[m][0]}", "🛢️", _wti(m)) for m in MODELS if m in MODEL_INFO)


COLLECTORS = (("nfl", "NFL Polymarket", "🏈", _nfl), ("btc", "BTC spot (Coinbase)", "₿", _btc),
              ("updown", "Crypto Up/Down 15m", "🎲", _updown), ("gold", "Gold Up/Down", "🥇", _gold),
              *_wti_collectors())


def collect(now: datetime | None = None, collectors=COLLECTORS, limit: int = 40):
    """Return ({desks: [...], totals: {...}}, trades) — each desk isolated from the others' failures."""
    now = now or datetime.now(timezone.utc)
    desks, trades = [], []
    for desk_id, name, emoji, fn in collectors:
        try:
            desk, desk_trades = fn(now, limit)
        except Exception as exc:  # one broken journal must not hide the other bots
            logger.warning("Tracker could not read %s: %s", desk_id, exc)
            desk, desk_trades = {"id": desk_id, "name": name, "emoji": emoji, "state": "down",
                                 "heartbeat_age_s": None, "notes": [f"could not read: {type(exc).__name__}"]}, []
        desks.append(desk)
        trades.extend(desk_trades)
    trades.sort(key=lambda t: t.get("at") or "", reverse=True)
    num = lambda k: sum(d.get(k) or 0 for d in desks)  # noqa: E731
    totals = {"balance": round(num("balance"), 2), "starting_balance": round(num("starting_balance"), 2),
              "today_pnl": round(num("today_pnl"), 2), "open_count": int(num("open_count")),
              "at_risk": round(num("at_risk"), 2),
              "states": {s: sum(d["state"] == s for d in desks) for s in ("ok", "warn", "down")}}
    return {"mode": "PAPER_ONLY", "as_of": now.isoformat(), "desks": desks, "totals": totals}, trades


def summarize(trades):
    """Totals over every trade given (the full roster, not just the recent ones)."""
    count = lambda status: sum(t["status"] == status for t in trades)  # noqa: E731
    closed = [t for t in trades if t["status"] in CLOSED_STATUSES]
    staked = sum(t.get("stake") or 0 for t in closed)
    net = sum(t["pnl"] or 0 for t in closed)
    decided = sum(t["pnl"] != 0 for t in closed)
    return {"open": count("OPEN") + count("WORKING"), "wins": count("WIN"), "losses": count("LOSS"),
            "outs": count("OUT"), "targets": count("TARGET"), "stops": count("STOP"), "flat": count("FLAT"),
            "closed": len(closed), "net_pnl": round(net, 4),
            "staked": round(staked, 4), "roi": round(net / staked, 4) if staked else None,
            "win_rate": round(sum((t["pnl"] or 0) > 0 for t in closed) / decided, 4) if decided else None}


def roster(now: datetime | None = None, collectors=COLLECTORS, limit: int = 500, desk: str | None = None):
    """Every desk with all of its trades (newest first) and totals over them."""
    status, trades = collect(now, [c for c in collectors if desk in (None, c[0])], limit)
    out = []
    for d in status["desks"]:
        mine = [t for t in trades if t["desk"] == d["id"]]
        out.append({"id": d["id"], "name": d["name"], "emoji": d["emoji"], "state": d["state"],
                    "balance": d.get("balance"), "starting_balance": d.get("starting_balance"),
                    "summary": summarize(mine), "trades": mine})
    every = [t for t in trades]
    return {"mode": "PAPER_ONLY", "as_of": status["as_of"], "desks": out, "totals": summarize(every)}
