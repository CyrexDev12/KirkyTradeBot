"""Event-sourced paper execution engine (conservative observable-book mode).

Every state change is an event with a deterministic ID. The account, orders
and positions are a fold over the event journal, so a restart rebuilds the
exact state, a duplicate event is ignored, and replaying identical snapshots
with an identical configuration produces identical events.

Execution rules implemented here (see docs/PAPER_TRADING.md):

* Entry buys fill only on fresh, open books observed at least ``latency_seconds``
  after activation, only against selected-outcome asks at or below the limit,
  and only up to ``entry_participation_cap`` of unconsumed displayed size. Fills
  are booked at the limit price (no assumed price improvement) with taker fees.
* Target sales fill only against selected-outcome bids at or above the target,
  booked at the target price with taker fees and the same participation cap.
* A stop activates when the fresh best selected bid is at or below the stop
  trigger. Stop and time exits walk the observable bids at their actual prices;
  a gap below the stop is booked at the lower executable levels, and with no
  bids the position stays open.
* Displayed liquidity is consumed once. A canonical price level that supplies
  any simulated fill is treated as fully spent at the size displayed at that
  moment; it can supply more only after it is observed with a larger displayed
  size (fresh replenishment), and only the increase is usable. If the displayed
  size shrinks, the spent amount shrinks with it; a level that disappears resets.
* Any exit activity cancels remaining entry quantity first, so exposure is never
  reopened after a profit is taken. Target and stop share one inventory count.
* No fills are invented across a feed gap; gaps are journaled as outages.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal

from .book import NO, OPEN, RESOLVED, YES, BookSnapshot, is_fresh, selected_view
from .common import ONE, ZERO, D, floor_step, iso, utc
from .config import FROZEN_THESIS_PRICE_EXITS, PREGAME_ONLY, StrategyConfig
from .fees import TAKER, FeeProvider, execution_fee, fee_upper_bound

# Entry states
PROPOSED, WORKING, PARTIALLY_FILLED, FILLED = "PROPOSED", "WORKING", "PARTIALLY_FILLED", "FILLED"
CANCELED, EXPIRED, INVALIDATED = "CANCELED", "EXPIRED", "INVALIDATED"
ACTIVE_ENTRY = (WORKING, PARTIALLY_FILLED)
# Position states
POS_OPEN, EXIT_WORKING, PARTIALLY_CLOSED, CLOSED, SETTLEMENT_PENDING = (
    "OPEN", "EXIT_WORKING", "PARTIALLY_CLOSED", "CLOSED", "SETTLEMENT_PENDING")
# Exit reasons
TARGET, STOP, TIME, SETTLEMENT = "TARGET", "STOP", "TIME", "SETTLEMENT"
Q8 = Decimal("0.00000001")


class EngineError(RuntimeError):
    pass


def deadlines(kickoff: datetime, policy: str, cfg: StrategyConfig) -> tuple[datetime, datetime]:
    entry_expires = kickoff - timedelta(minutes=cfg.entry_expiry_minutes_before_kickoff)
    if policy == PREGAME_ONLY:
        return entry_expires, kickoff - timedelta(minutes=cfg.pregame_exit_minutes_before_kickoff)
    if policy == FROZEN_THESIS_PRICE_EXITS:
        return entry_expires, kickoff + timedelta(hours=cfg.frozen_thesis_exit_hours_after_kickoff)
    raise ValueError(policy)


@dataclass(frozen=True)
class TradePlan:
    trade_id: str
    revision: int
    strategy_id: str
    config_version: str
    season: int
    week: int
    game_id: str
    market_slug: str
    market_type: str
    selected_outcome: str
    selected_side: str
    total_line: Decimal | None
    settlement_rule_version: str
    hold_policy: str
    kickoff: datetime
    decision_at: datetime
    entry_active_after: datetime
    entry_expires_at: datetime
    exit_deadline: datetime
    entry_limit: Decimal
    target_limit: Decimal
    stop_trigger: Decimal
    quantity: Decimal
    tick_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    estimated_entry_cost: Decimal
    planned_loss: Decimal
    full_funded_loss: Decimal
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.selected_side not in (YES, NO):
            raise ValueError("selected_side must be YES or NO")
        if not ZERO < self.stop_trigger < self.entry_limit < self.target_limit < ONE:
            raise ValueError("Require 0 < stop < entry < target < 1")
        if self.quantity <= 0:
            raise ValueError("Quantity must be positive")
        if not self.entry_active_after <= self.entry_expires_at <= self.kickoff:
            raise ValueError("Entry window must end at or before kickoff")

    def to_dict(self) -> dict:
        out = {}
        for k, v in asdict(self).items():
            out[k] = str(v) if isinstance(v, Decimal) else iso(v) if isinstance(v, datetime) else v
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "TradePlan":
        dec = {"total_line", "entry_limit", "target_limit", "stop_trigger", "quantity", "tick_size", "quantity_step",
               "min_quantity", "estimated_entry_cost", "planned_loss", "full_funded_loss"}
        dts = {"kickoff", "decision_at", "entry_active_after", "entry_expires_at", "exit_deadline"}
        kw = {}
        for k, v in d.items():
            if k in dec:
                kw[k] = D(v) if v is not None else None
            elif k in dts:
                kw[k] = utc(v)
            else:
                kw[k] = v
        return cls(**kw)


@dataclass
class Order:
    order_id: str
    trade_id: str
    quantity: Decimal
    limit: Decimal
    status: str = WORKING
    filled: Decimal = ZERO
    reserved: Decimal = ZERO
    activated_at: datetime | None = None
    fills: int = 0
    closed_reason: str | None = None


@dataclass
class Position:
    trade_id: str
    qty_open: Decimal = ZERO
    qty_bought: Decimal = ZERO
    cost_basis: Decimal = ZERO  # remaining allocated cost including entry fees
    total_cost: Decimal = ZERO
    status: str = POS_OPEN
    exit_mode: str | None = None
    exit_started: bool = False
    last_entry_fill_at: datetime | None = None
    realized: Decimal = ZERO
    proceeds: Decimal = ZERO
    entry_fees: Decimal = ZERO
    exit_fees: Decimal = ZERO
    exit_qty: dict = field(default_factory=dict)


@dataclass
class AccountState:
    account_id: str | None = None
    opened_at: datetime | None = None
    starting_equity: Decimal = ZERO
    cash: Decimal = ZERO
    plans: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)
    started_games: set = field(default_factory=set)
    outages: list = field(default_factory=list)
    paused: bool = False
    pause_reason: str | None = None
    review_reference_equity: Decimal | None = None
    review_at: str | None = None
    week_open: dict = field(default_factory=dict)
    seen: set = field(default_factory=set)
    event_count: int = 0

    @property
    def reserved(self) -> Decimal:
        return sum((o.reserved for o in self.orders.values() if o.status in ACTIVE_ENTRY), ZERO)

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved


def _plan_order_id(trade_id: str) -> str:
    return f"{trade_id}:entry"


class PaperEngine:
    """Deterministic paper engine. Call ``take_events()`` after each input and journal them."""

    def __init__(self, config: StrategyConfig, fees: FeeProvider | None = None):
        self.cfg = config
        self.fees = fees or FeeProvider(profile_id=config.fee_profile_id)
        self.state = AccountState()
        self.memory = {"liquidity": {}, "last_received": {}, "marks": {}, "last_snapshot": {}}
        self._pending: list[dict] = []

    # ------------------------------------------------------------------ events
    def take_events(self) -> list[dict]:
        events, self._pending = self._pending, []
        return events

    def _emit(self, etype: str, ts: datetime, key: str, trade_id: str | None = None, **data) -> dict | None:
        event_id = f"{etype}:{trade_id or '-'}:{key}"
        if event_id in self.state.seen:
            return None
        event = {"event_id": event_id, "type": etype, "ts": iso(ts), "trade_id": trade_id, **data}
        self.apply(event)
        self._pending.append(event)
        return event

    def apply(self, e: dict) -> None:
        """Fold one journal event into state. Must stay free of external inputs."""
        s = self.state
        if e["event_id"] in s.seen:
            return
        s.seen.add(e["event_id"])
        s.event_count += 1
        t, tid = e["type"], e.get("trade_id")
        ts = utc(e["ts"])
        if t == "ACCOUNT_OPENED":
            if s.account_id is not None:
                raise EngineError("Account already opened")
            s.account_id, s.opened_at = e["account_id"], ts
            s.starting_equity = s.cash = D(e["cash"])
        elif t == "PLAN_ACCEPTED":
            s.plans[tid] = TradePlan.from_dict(e["plan"])
        elif t == "ENTRY_WORKING":
            p = s.plans[tid]
            s.orders[tid] = Order(order_id=_plan_order_id(tid), trade_id=tid, quantity=p.quantity,
                                  limit=p.entry_limit, reserved=D(e["reserve"]), activated_at=ts)
        elif t == "ENTRY_FILL":
            o = s.orders[tid]
            qty, cost, fee = D(e["quantity"]), D(e["cost"]), D(e["fee"])
            o.filled += qty
            o.fills += 1
            o.reserved -= D(e["drawn"]) + D(e["released"])
            o.status = FILLED if o.filled >= o.quantity else PARTIALLY_FILLED
            s.cash -= cost
            pos = s.positions.setdefault(tid, Position(trade_id=tid))
            pos.qty_open += qty
            pos.qty_bought += qty
            pos.cost_basis += cost
            pos.total_cost += cost
            pos.entry_fees += fee
            pos.last_entry_fill_at = ts
        elif t == "ENTRY_CLOSED":
            o = s.orders[tid]
            o.status, o.closed_reason, o.reserved = e["status"], e["reason"], ZERO
        elif t == "EXIT_ACTIVATED":
            pos = s.positions[tid]
            pos.exit_started = True
            if e["mode"] in (STOP, TIME):
                pos.exit_mode = e["mode"]
                pos.status = EXIT_WORKING
        elif t in ("EXIT_FILL", "SETTLED"):
            pos = s.positions[tid]
            qty = D(e["quantity"])
            pos.exit_started = True
            pos.qty_open -= qty
            pos.cost_basis -= D(e["cost_allocated"])
            s.cash += D(e["net_proceeds"])
            pos.proceeds += D(e["net_proceeds"])
            pos.exit_fees += D(e.get("fee", "0"))
            pos.realized += D(e["realized"])
            reason = e.get("reason", SETTLEMENT)
            pos.exit_qty[reason] = str(D(pos.exit_qty.get(reason, "0")) + qty)
            if pos.qty_open <= 0:
                pos.status = CLOSED
            elif t == "EXIT_FILL" and pos.status not in (EXIT_WORKING,):
                pos.status = PARTIALLY_CLOSED
        elif t == "SETTLEMENT_PENDING":
            s.positions[tid].status = SETTLEMENT_PENDING
        elif t == "GAME_STARTED":
            s.started_games.add(e["game_id"])
        elif t == "DEADLINES_REVISED":
            p = s.plans[tid]
            s.plans[tid] = replace(p, kickoff=utc(e["kickoff"]), entry_expires_at=utc(e["entry_expires_at"]),
                                   exit_deadline=utc(e["exit_deadline"]), revision=p.revision + 1)
        elif t == "OUTAGE":
            s.outages.append({k: e[k] for k in ("slug", "gap_start", "gap_end", "seconds")})
        elif t == "PAUSED":
            s.paused, s.pause_reason = True, e["reason"]
        elif t == "RESUMED":
            s.paused, s.pause_reason = False, None
        elif t == "REVIEW_RECORDED":
            s.review_reference_equity, s.review_at = D(e["equity"]), e["ts"]
        elif t == "WEEK_OPENED":
            s.week_open[f"{e['season']}-{e['week']}"] = D(e["equity"])
        else:
            raise EngineError(f"Unknown event type {t}")

    # --------------------------------------------------------------- commands
    def open_account(self, account_id: str, ts: datetime, equity: Decimal | None = None):
        if self.state.account_id is not None:
            return None  # never reset or replenish an existing account
        return self._emit("ACCOUNT_OPENED", ts, account_id, account_id=account_id,
                          cash=str(equity if equity is not None else self.cfg.starting_equity_usd))

    def pause(self, ts: datetime, reason: str, actor: str):
        return self._emit("PAUSED", ts, iso(ts), reason=reason, actor=actor)

    def resume(self, ts: datetime, actor: str):
        return self._emit("RESUMED", ts, iso(ts), actor=actor)

    def record_review(self, ts: datetime, equity: Decimal, note: str, actor: str):
        return self._emit("REVIEW_RECORDED", ts, iso(ts), equity=str(equity), note=note, actor=actor)

    def open_week(self, season: int, week: int, ts: datetime, equity: Decimal):
        return self._emit("WEEK_OPENED", ts, f"{season}-{week}", season=season, week=week, equity=str(equity))

    def theta(self, ts: datetime, market_type: str) -> Decimal:
        return self.fees.profile_at(ts, market_type).theta(TAKER)

    def entry_reserve(self, plan: TradePlan, ts: datetime) -> Decimal:
        theta = self.theta(ts, plan.market_type)
        return plan.quantity * plan.entry_limit + fee_upper_bound(theta, plan.entry_limit, plan.quantity)

    def submit(self, plan: TradePlan, ts: datetime) -> dict:
        """Activate a local paper entry. Returns {accepted, reason}. Never contacts a venue."""
        s = self.state
        if s.account_id is None:
            raise EngineError("Account not opened")
        if plan.trade_id in s.plans:
            return {"accepted": False, "reason": "duplicate trade_id"}
        if s.paused:
            return {"accepted": False, "reason": "paper trading paused"}
        if plan.game_id in s.started_games or ts >= plan.entry_expires_at:
            return {"accepted": False, "reason": "entry window closed"}
        reserve = self.entry_reserve(plan, ts)
        if reserve > s.available_cash:
            return {"accepted": False, "reason": "insufficient available cash"}
        self._emit("PLAN_ACCEPTED", ts, str(plan.revision), plan.trade_id, plan=plan.to_dict())
        self._emit("ENTRY_WORKING", max(ts, plan.entry_active_after), "1", plan.trade_id, reserve=str(reserve))
        return {"accepted": True, "reason": None, "reserve": reserve}

    def cancel_entry(self, trade_id: str, ts: datetime, status: str, reason: str):
        o = self.state.orders.get(trade_id)
        if o and o.status in ACTIVE_ENTRY:
            self._emit("ENTRY_CLOSED", ts, "closed", trade_id, status=status, reason=reason,
                       released=str(o.reserved))

    # ------------------------------------------------------------ market data
    def _levels(self, snap: BookSnapshot, side: str, action: str):
        """Selected-outcome levels with their canonical book key, best first.

        action "buy" consumes selected asks; "sell" consumes selected bids.
        """
        if (side, action) in ((YES, "buy"), (NO, "sell")):
            canon, rows = "offers", snap.offers
        else:
            canon, rows = "bids", snap.bids
        out = []
        for price, qty in rows:
            selected = price if side == YES else ONE - price
            out.append((selected, canon, price, qty))
        return out  # canonical ordering already gives selected best-first for each case

    def _available(self, slug: str, canon: str, price: Decimal, displayed: Decimal) -> Decimal:
        used = D(self.memory["liquidity"].get(slug, {}).get(f"{canon}:{price}", "0"))
        return max(ZERO, displayed - used)

    def _consume(self, slug: str, canon: str, price: Decimal, displayed: Decimal) -> None:
        """Mark the whole displayed size of a level that supplied a fill as spent."""
        book = self.memory["liquidity"].setdefault(slug, {})
        key = f"{canon}:{price}"
        book[key] = str(max(D(book.get(key, "0")), displayed))

    def _refresh_liquidity(self, snap: BookSnapshot) -> None:
        book = self.memory["liquidity"].get(snap.slug)
        if not book:
            return
        visible = {f"bids:{p}": q for p, q in snap.bids} | {f"offers:{p}": q for p, q in snap.offers}
        for key in list(book):
            if key not in visible:
                del book[key]  # level gone: earlier consumption no longer constrains it
            elif visible[key] < D(book[key]):
                book[key] = str(visible[key])  # displayed size fell; keep only what is still visible

    def _active_trades(self, slug: str):
        s = self.state
        for tid in sorted(s.plans):
            p = s.plans[tid]
            if p.market_slug != slug:
                continue
            o, pos = s.orders.get(tid), s.positions.get(tid)
            if (o and o.status in ACTIVE_ENTRY) or (pos and pos.qty_open > 0):
                yield p

    def on_snapshot(self, snap: BookSnapshot) -> None:
        now = snap.received_at
        last = self.memory["last_received"].get(snap.slug)
        if last is not None and utc(last) >= now:
            return  # duplicate or out-of-order snapshot; never reuse it
        trades = list(self._active_trades(snap.slug))
        if last is not None and trades:
            gap = (now - utc(last)).total_seconds()
            if gap > self.cfg.outage_gap_seconds:
                self._emit("OUTAGE", now, f"{snap.slug}:{last}", None, slug=snap.slug, gap_start=last,
                           gap_end=iso(now), seconds=gap)
        self._refresh_liquidity(snap)
        self.memory["last_received"][snap.slug] = iso(now)
        self.memory["last_snapshot"][snap.slug] = snap.to_dict()
        self.on_clock(now)
        fresh, _ = is_fresh(snap, now, self.cfg.max_quote_age_seconds)
        for plan in trades:
            self._exits(plan, snap, fresh)
            self._entry(plan, snap, fresh)
            self._mark(plan, snap)

    def _entry(self, plan: TradePlan, snap: BookSnapshot, fresh: bool) -> None:
        s, now = self.state, snap.received_at
        o = s.orders.get(plan.trade_id)
        if not o or o.status not in ACTIVE_ENTRY:
            return
        pos = s.positions.get(plan.trade_id)
        if pos and pos.exit_started:
            self.cancel_entry(plan.trade_id, now, CANCELED, "exit activity began")
            return
        if not fresh or snap.state != OPEN or plan.game_id in s.started_games:
            return
        if now < o.activated_at + timedelta(seconds=self.cfg.latency_seconds) or now >= plan.entry_expires_at:
            return
        remaining = o.quantity - o.filled
        take, used = ZERO, []
        for price, canon, cprice, displayed in self._levels(snap, plan.selected_side, "buy"):
            if price > plan.entry_limit or take >= remaining:
                break
            avail = floor_step(self._available(snap.slug, canon, cprice, displayed) * self.cfg.entry_participation_cap,
                               plan.quantity_step)
            q = min(avail, remaining - take)
            if q > 0:
                used.append((canon, cprice, q, displayed))
                take += q
        take = floor_step(take, plan.quantity_step)
        if take <= 0:
            return
        theta = self.theta(now, plan.market_type)
        while take > 0:
            fee = execution_fee(theta, [(plan.entry_limit, take)])
            cost = take * plan.entry_limit + fee
            if cost <= o.reserved + max(ZERO, s.available_cash):
                break
            take -= plan.quantity_step
        if take <= 0:
            return
        drawn = min(cost, o.reserved)
        filled_after = o.filled + take
        released = (o.reserved - drawn) if filled_after >= o.quantity else ZERO
        left = take
        for canon, cprice, q, displayed in used:
            if left > 0:
                self._consume(snap.slug, canon, cprice, displayed)
                left -= min(q, left)
        self._emit("ENTRY_FILL", now, str(o.fills + 1), plan.trade_id, price=str(plan.entry_limit),
                   quantity=str(take), fee=str(fee), cost=str(cost), drawn=str(drawn), released=str(released),
                   snapshot_received_at=iso(now), fee_role=TAKER, theta=str(theta))

    def _sell(self, plan: TradePlan, snap: BookSnapshot, qty: Decimal, floor: Decimal | None, at_limit: bool,
              cap: Decimal | None) -> list:
        """Fills for selling ``qty`` into selected bids; ``floor`` limits the lowest acceptable bid."""
        fills, left = [], qty
        for price, canon, cprice, displayed in self._levels(snap, plan.selected_side, "sell"):
            if left <= 0 or (floor is not None and price < floor):
                break
            avail = self._available(snap.slug, canon, cprice, displayed)
            if cap is not None:
                avail = avail * cap
            q = min(floor_step(avail, plan.quantity_step), left)
            if q > 0:
                fills.append((plan.target_limit if at_limit else price, q, canon, cprice, displayed))
                left -= q
        return fills

    def _book_exit(self, plan: TradePlan, now: datetime, reason: str, fills: list) -> None:
        pos = self.state.positions[plan.trade_id]
        qty = sum((f[1] for f in fills), ZERO)
        if qty <= 0:
            return
        for _, q, canon, cprice, displayed in fills:
            self._consume(plan.market_slug, canon, cprice, displayed)
        theta = self.theta(now, plan.market_type)
        gross = sum((f[0] * f[1] for f in fills), ZERO)
        fee = execution_fee(theta, [(f[0], f[1]) for f in fills])
        alloc = pos.cost_basis if qty >= pos.qty_open else (pos.cost_basis * qty / pos.qty_open).quantize(Q8)
        net = gross - fee
        n = sum(1 for k in self.state.seen if k.startswith(f"EXIT_FILL:{plan.trade_id}:"))
        self._emit("EXIT_FILL", now, str(n + 1), plan.trade_id, reason=reason,
                   fills=[[str(f[0]), str(f[1])] for f in fills], quantity=str(qty), gross=str(gross),
                   fee=str(fee), net_proceeds=str(net), cost_allocated=str(alloc), realized=str(net - alloc),
                   fee_role=TAKER, theta=str(theta))

    def _exits(self, plan: TradePlan, snap: BookSnapshot, fresh: bool) -> None:
        s, now = self.state, snap.received_at
        pos = s.positions.get(plan.trade_id)
        if not pos or pos.qty_open <= 0 or pos.status in (CLOSED, SETTLEMENT_PENDING):
            return
        if snap.state == RESOLVED:
            self.cancel_entry(plan.trade_id, now, CANCELED, "market resolved")
            self._emit("SETTLEMENT_PENDING", now, "pending", plan.trade_id)
            return
        if not fresh or snap.state != OPEN:
            return  # halted/closed/stale: a deadline or trigger cannot manufacture a fill
        if pos.last_entry_fill_at and now < pos.last_entry_fill_at + timedelta(seconds=self.cfg.latency_seconds):
            return
        view = selected_view(snap, plan.selected_side)
        if pos.exit_mode is None:
            if view.best_bid is not None and view.best_bid <= plan.stop_trigger:
                self.cancel_entry(plan.trade_id, now, CANCELED, "stop activated")
                self._emit("EXIT_ACTIVATED", now, STOP, plan.trade_id, mode=STOP,
                           trigger_bid=str(view.best_bid))
            else:
                fills = self._sell(plan, snap, pos.qty_open, plan.target_limit, True,
                                   self.cfg.entry_participation_cap)
                if fills:
                    self.cancel_entry(plan.trade_id, now, CANCELED, "target exit began")
                    if not pos.exit_started:
                        self._emit("EXIT_ACTIVATED", now, TARGET, plan.trade_id, mode=TARGET)
                    self._book_exit(plan, now, TARGET, fills)
                return
        if pos.exit_mode in (STOP, TIME) and pos.qty_open > 0:
            self._book_exit(plan, now, pos.exit_mode, self._sell(plan, snap, pos.qty_open, None, False, None))

    def _mark(self, plan: TradePlan, snap: BookSnapshot) -> None:
        pos = self.state.positions.get(plan.trade_id)
        if not pos or pos.qty_open <= 0:
            self.memory["marks"].pop(plan.trade_id, None)
            return
        fills = self._sell(plan, snap, pos.qty_open, None, False, None)
        qty = sum((f[1] for f in fills), ZERO)
        theta = self.theta(snap.received_at, plan.market_type)
        net = sum((f[0] * f[1] for f in fills), ZERO) - execution_fee(theta, [(f[0], f[1]) for f in fills])
        view = selected_view(snap, plan.selected_side)
        self.memory["marks"][plan.trade_id] = {
            "at": iso(snap.received_at), "net_liquidation": str(net), "liquidatable_quantity": str(qty),
            "unfillable_residual": str(pos.qty_open - qty),
            "indicative_bid": str(view.best_bid) if view.best_bid is not None else None,
            "market_state": snap.state}

    # ----------------------------------------------------------- clock/status
    def on_clock(self, now: datetime) -> None:
        s = self.state
        for tid in sorted(s.plans):
            plan = s.plans[tid]
            o, pos = s.orders.get(tid), s.positions.get(tid)
            if o and o.status in ACTIVE_ENTRY and now >= plan.entry_expires_at:
                self.cancel_entry(tid, now, EXPIRED, "entry expiry reached")
            if pos and pos.qty_open > 0 and pos.exit_mode is None and pos.status != SETTLEMENT_PENDING \
                    and now >= plan.exit_deadline:
                self.cancel_entry(tid, now, CANCELED, "time exit activated")
                self._emit("EXIT_ACTIVATED", now, TIME, tid, mode=TIME)

    def on_game_started(self, game_id: str, ts: datetime) -> None:
        self._emit("GAME_STARTED", ts, game_id, None, game_id=game_id)
        for tid in sorted(self.state.plans):
            if self.state.plans[tid].game_id == game_id:
                self.cancel_entry(tid, ts, CANCELED, "game observed started")

    def on_kickoff_change(self, game_id: str, new_kickoff: datetime, ts: datetime) -> None:
        """Verified pre-start rescheduling only. Forecasts stay frozen; only deadlines move."""
        if game_id in self.state.started_games:
            return
        for tid in sorted(self.state.plans):
            p = self.state.plans[tid]
            if p.game_id != game_id or p.kickoff == new_kickoff or ts >= p.kickoff:
                continue
            entry_exp, exit_dl = deadlines(new_kickoff, p.hold_policy, self.cfg)
            self._emit("DEADLINES_REVISED", ts, iso(new_kickoff), tid, kickoff=iso(new_kickoff),
                       entry_expires_at=iso(entry_exp), exit_deadline=iso(exit_dl), previous_kickoff=iso(p.kickoff))

    def on_settlement(self, slug: str, selected_payouts: dict, ts: datetime, source: str) -> None:
        """``selected_payouts`` maps trade_id -> verified payout per selected contract."""
        for tid in sorted(self.state.plans):
            plan = self.state.plans[tid]
            if plan.market_slug != slug:
                continue
            self.cancel_entry(tid, ts, CANCELED, "market settled")
            pos = self.state.positions.get(tid)
            if not pos or pos.qty_open <= 0 or tid not in selected_payouts:
                continue
            payout = D(selected_payouts[tid])
            gross = pos.qty_open * payout
            self._emit("SETTLED", ts, "final", tid, payout=str(payout), quantity=str(pos.qty_open),
                       gross=str(gross), fee="0", net_proceeds=str(gross), cost_allocated=str(pos.cost_basis),
                       realized=str(gross - pos.cost_basis), source=source, reason=SETTLEMENT)
            self.memory["marks"].pop(tid, None)

    # --------------------------------------------------------------- reports
    def account(self, now: datetime) -> dict:
        s = self.state
        positions, liq, unreal, provisional = [], ZERO, ZERO, False
        for tid in sorted(s.positions):
            pos, plan = s.positions[tid], s.plans[tid]
            if pos.qty_open <= 0:
                continue
            mark = self.memory["marks"].get(tid)
            stale = (mark is None or (now - utc(mark["at"])).total_seconds() > self.cfg.max_quote_age_seconds
                     or pos.status == SETTLEMENT_PENDING)
            value = D(mark["net_liquidation"]) if mark else ZERO
            provisional |= stale
            liq += value
            unreal += value - pos.cost_basis
            positions.append({"trade_id": tid, "game_id": plan.game_id, "selected_outcome": plan.selected_outcome,
                              "status": pos.status, "quantity": str(pos.qty_open), "cost_basis": str(pos.cost_basis),
                              "net_liquidation_value": str(value), "mark": mark, "provisional": stale})
        realized = sum((p.realized for p in s.positions.values()), ZERO)
        fees = sum((p.entry_fees + p.exit_fees for p in s.positions.values()), ZERO)
        working = [{"trade_id": o.trade_id, "status": o.status, "remaining": str(o.quantity - o.filled),
                    "limit": str(o.limit), "reserved": str(o.reserved)}
                   for o in s.orders.values() if o.status in ACTIVE_ENTRY]
        return {"account_id": s.account_id, "as_of": iso(now), "cash": str(s.cash), "reserved": str(s.reserved),
                "available_cash": str(s.available_cash), "open_liquidation_value": str(liq),
                "equity": str(s.cash + liq), "realized_pnl": str(realized), "unrealized_pnl": str(unreal),
                "fees_paid": str(fees), "starting_equity": str(s.starting_equity), "positions": positions,
                "working_entries": working, "paused": s.paused, "pause_reason": s.pause_reason,
                "provisional": provisional, "outages": len(s.outages)}

    def equity(self, now: datetime) -> Decimal:
        return D(self.account(now)["equity"])
