"""Deterministic worked mock run of the paper engine and ledger.

Hypothetical fixture books only: no real game, quote, probability or
recommendation. It demonstrates trade IDs, partial entry fills, liquidity
consumption and replenishment, a partial target exit, a stop that gaps below
its trigger, an expired unfilled entry, settlement, and a full reconciliation
of cash against the event journal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tempfile

from .book import BookSnapshot
from .common import ZERO, D
from .config import StrategyConfig
from .paper_engine import TradePlan, deadlines
from .store import Store

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
KICK = T0 + timedelta(hours=6)


def _plan(cfg, trade_id, slug, outcome, side, entry, target, stop, qty, market_type="moneyline", line=None):
    exp, exit_dl = deadlines(KICK, cfg.hold_policy, cfg)
    return TradePlan(trade_id=trade_id, revision=1, strategy_id=cfg.strategy_id, config_version=cfg.version,
                     season=2026, week=12, game_id=f"MOCK_{slug.upper()}", market_slug=slug, market_type=market_type,
                     selected_outcome=outcome, selected_side=side, total_line=D(line) if line else None,
                     settlement_rule_version="mock", hold_policy=cfg.hold_policy, kickoff=KICK, decision_at=T0,
                     entry_active_after=T0, entry_expires_at=exp, exit_deadline=exit_dl, entry_limit=D(entry),
                     target_limit=D(target), stop_trigger=D(stop), quantity=D(qty), tick_size=D("0.01"),
                     quantity_step=D(1), min_quantity=D(1), estimated_entry_cost=ZERO, planned_loss=ZERO,
                     full_funded_loss=ZERO, meta={"mock": True})


def _snap(slug, seconds, bids, offers, state="OPEN"):
    t = T0 + timedelta(seconds=seconds)
    return BookSnapshot(slug, t, t, state, tuple((D(p), D(q)) for p, q in bids),
                        tuple((D(p), D(q)) for p, q in offers))


def run(db_path: str | None = None) -> dict:
    cfg = StrategyConfig()
    path = db_path or tempfile.mkstemp(suffix=".sqlite3")[1]
    store = Store(path)
    engine = store.load_engine(cfg)
    engine.open_account("mock-account", T0)
    plans = [
        _plan(cfg, "MOCK-001", "colts", "IND", "YES", "0.15", "0.30", "0.11", 40),
        _plan(cfg, "MOCK-002", "total-41-5", "under", "NO", "0.40", "0.50", "0.36", 10, "total", "41.5"),
        _plan(cfg, "MOCK-003", "bills", "BUF", "YES", "0.60", "0.75", "0.55", 5),
    ]
    for p in plans:
        engine.submit(p, T0)
    store.commit(engine, T0)
    tape = [
        # MOCK-001: only 40 displayed at 15c -> 50% participation fills 20 (partial entry).
        _snap("colts", 5, [("0.13", 100)], [("0.15", 40)]),
        # Same displayed size again: the level already supplied a fill, so nothing more fills.
        _snap("colts", 10, [("0.13", 100)], [("0.15", 40)]),
        # Replenished to 80: only the 40-contract increase is fresh; 50% of it fills the last 20.
        _snap("colts", 15, [("0.13", 100)], [("0.15", 80)]),
        # Bid reaches the 30c target with 30 displayed: 50% cap sells 15 (partial target exit).
        _snap("colts", 25, [("0.30", 30), ("0.28", 50)], [("0.31", 20)]),
        # Bid collapses through the 11c stop: next executable levels are 7c and 6c (gap fill).
        _snap("colts", 35, [("0.07", 10), ("0.06", 50)], [("0.09", 20)]),
        # MOCK-002 (Under = NO side): selected ask = 1 - YES bid = 0.45 > 0.40 limit; never fills.
        _snap("total-41-5", 5, [("0.55", 100)], [("0.57", 100)]),
        # MOCK-003: entry fills in full; no exit level is touched before resolution.
        _snap("bills", 5, [("0.58", 100)], [("0.60", 100)]),
        _snap("bills", 20, [("0.62", 100)], [("0.64", 100)]),
    ]
    for snap in sorted(tape, key=lambda s: (s.received_at, s.slug)):
        engine.on_snapshot(snap)
        store.commit(engine, snap.received_at, write_memory=True)
    expiry = KICK - timedelta(minutes=cfg.entry_expiry_minutes_before_kickoff)
    engine.on_clock(expiry)
    store.commit(engine, expiry, write_memory=True)
    settle_at = KICK + timedelta(hours=3, minutes=30)
    engine.on_settlement("bills", {"MOCK-003": Decimal("1")}, settle_at, "mock final score")
    store.commit(engine, settle_at, write_memory=True)
    # Restart: rebuild entirely from the journal and compare.
    rebuilt = store.load_engine(cfg)
    account = rebuilt.account(settle_at)
    events = store.ledger_events()
    cash = ZERO
    for e in events:
        if e["type"] == "ACCOUNT_OPENED":
            cash += D(e["cash"])
        elif e["type"] == "ENTRY_FILL":
            cash -= D(e["cost"])
        elif e["type"] in ("EXIT_FILL", "SETTLED"):
            cash += D(e["net_proceeds"])
    realized = sum((p.realized for p in rebuilt.state.positions.values()), ZERO)
    return {"db_path": path, "events": events, "account": account, "journal_cash": cash,
            "reconciled": cash == D(account["cash"]) and D(account["equity"]) == D("100.00") + realized
            and D(account["reserved"]) == ZERO,
            "positions": {tid: {"status": p.status, "bought": str(p.qty_bought), "realized": str(p.realized),
                                "exit_quantities": p.exit_qty} for tid, p in rebuilt.state.positions.items()},
            "orders": {tid: {"status": o.status, "filled": str(o.filled), "reason": o.closed_reason}
                       for tid, o in rebuilt.state.orders.items()}}


def markdown(result: dict) -> str:
    lines = ["# Worked paper-trading mock run", "",
             "Hypothetical fixture books only. Not an assessed trade, quote, probability or recommendation.",
             "Generated by `python -m backend.trading.cli mock-run`.", "",
             "| seq | event | trade | detail |", "| --- | --- | --- | --- |"]
    for e in result["events"]:
        keys = [k for k in ("reason", "mode", "status", "quantity", "price", "fills", "fee", "cost", "net_proceeds",
                            "realized", "reserve", "released", "payout", "cash") if k in e]
        detail = ", ".join(f"{k}={e[k]}" for k in keys)
        lines.append(f"| {e['seq']} | {e['type']} | {e.get('trade_id') or ''} | {detail} |")
    a = result["account"]
    lines += ["", "## Final state", "",
              f"* Orders: {result['orders']}", f"* Positions: {result['positions']}",
              f"* Cash {a['cash']} · reserved {a['reserved']} · equity {a['equity']} · realized {a['realized_pnl']}"
              f" · fees {a['fees_paid']}",
              f"* Cash recomputed independently from the journal: {result['journal_cash']}",
              f"* Reconciled after rebuilding from the journal: **{result['reconciled']}**"]
    return "\n".join(lines) + "\n"
