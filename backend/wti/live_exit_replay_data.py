"""Read the confirmed Oil D live positions and official WTI market payouts for exit replay."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from math import isfinite
from pathlib import Path
import sqlite3


ZERO = Decimal(0)


@dataclass(frozen=True)
class LiveFill:
    quantity: Decimal
    price: Decimal
    fee: Decimal
    at: float
    exchange_at: float | None
    observed_at: float | None


@dataclass(frozen=True)
class LiveOrder:
    id: int
    kind: str
    state: str
    limit_price: Decimal
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    fees: Decimal | None
    prepared_at: float
    sending_at: float | None
    fills: tuple[LiveFill, ...]
    expires_at: float | None
    cancel_requested_at: float | None
    reason: str | None

    @property
    def first_exchange_fill_at(self) -> float | None:
        return min((fill.exchange_at for fill in self.fills if fill.exchange_at is not None), default=None)

    @property
    def last_exchange_fill_at(self) -> float | None:
        return max((fill.exchange_at for fill in self.fills if fill.exchange_at is not None), default=None)


@dataclass(frozen=True)
class LiveTrade:
    id: int
    slug: str
    side: str
    signal_at: float
    window_start: int
    window_end: int
    entry: LiveOrder
    exits: tuple[LiveOrder, ...]
    net_pnl: Decimal
    fees: Decimal | None
    fees_exact: bool | None
    payout_total: Decimal | None
    exit_reason: str | None
    closed_at: float

    @property
    def remaining_quantity(self) -> Decimal:
        """Contracts remaining after the actual sell orders and before any payout."""
        return self.entry.filled_quantity - sum((order.filled_quantity for order in self.exits), ZERO)


@dataclass(frozen=True)
class TapeMarket:
    slug: str
    window_start: int
    window_end: int
    price_tick: Decimal | None
    minimum_quantity: Decimal | None
    yes_payout: Decimal | None
    settled_at: float | None


_TRADE_SCOPE = "t.state='CLOSED' AND t.closed_at BETWEEN ? AND ? AND t.window_end-t.window_start=900"


def _readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    return closing(db)


def _amount(value):
    if value is not None:
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            return None
        if result.is_finite():
            return result
    return None


def _stamp(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _fill(row):
    payload = json.loads(row["payload"])
    return LiveFill(quantity=Decimal(row["quantity"]), price=Decimal(row["price"]), fee=Decimal(row["fee"]),
                    at=row["at"], exchange_at=_stamp(payload.get("exchange_at")),
                    observed_at=_stamp(payload.get("observed_at")))


def _order(row, request, payload, sending, fills):
    order_id = row["id"]
    return LiveOrder(
        id=order_id, kind=row["kind"], state=row["state"], limit_price=Decimal(request["price"]),
        requested_quantity=Decimal(request["quantity"]), filled_quantity=_amount(payload.get("filled")) or ZERO,
        average_price=_amount(payload.get("average_price")), fees=_amount(payload.get("fees")),
        prepared_at=row["created_at"], sending_at=sending.get(order_id), fills=tuple(fills.get(order_id, ())),
        expires_at=_stamp(request.get("expires_at")), cancel_requested_at=_stamp(payload.get("cancel_requested_at")),
        reason=payload.get("reason"),
    )


def load_live_trades(path: Path, since: float, as_of: float) -> list[LiveTrade]:
    """Use the close timestamp for the reporting window and require confirmed stream entries."""
    path = Path(path)
    if not path.is_file():
        return []
    timestamps = (since, as_of)
    with _readonly(path) as db:
        db.execute("BEGIN")
        rows = db.execute(f"""SELECT t.*,s.at AS signal_at,s.payload AS signal FROM trades t
            JOIN signals s ON s.id=t.signal_id WHERE {_TRADE_SCOPE} ORDER BY t.closed_at,t.id""", timestamps).fetchall()
        if not rows:
            return []
        orders = defaultdict(list)
        for order in db.execute(f"""SELECT o.* FROM orders o JOIN trades t ON t.id=o.trade_id
                WHERE {_TRADE_SCOPE} ORDER BY o.id""", timestamps):
            orders[order["trade_id"]].append(order)
        fills = defaultdict(list)
        for fill in db.execute(f"""SELECT f.* FROM fills f JOIN orders o ON o.id=f.order_id
                JOIN trades t ON t.id=o.trade_id WHERE {_TRADE_SCOPE} AND f.at<=? ORDER BY f.at,f.execution_id""",
                (*timestamps, as_of)):
            fills[fill["order_id"]].append(_fill(fill))
        sending = dict(db.execute("""SELECT json_extract(payload,'$.order.id'), MIN(at) FROM events
                 WHERE kind='order_update' AND at BETWEEN ? AND ? AND json_extract(payload,'$.state')='sending'
                 GROUP BY json_extract(payload,'$.order.id')""", (min(row["created_at"] for row in rows), as_of)))

    result = []
    for row in rows:
        entry_rows = [order for order in orders[row["id"]] if order["kind"] == "entry"]
        if len(entry_rows) != 1:
            continue
        entry = entry_rows[0]
        request = json.loads(entry["request"])
        signal = json.loads(row["signal"])
        source = request.get("signal_source") or signal.get("signal_source")
        quote_source = request.get("quote_source")
        if (source != "stream" or quote_source not in (None, "stream") or not entry["venue_id"]
                or request.get("side") not in ("UP", "DOWN")):
            continue
        entry = _order(entry, request, json.loads(entry["payload"]), sending, fills)
        closed = json.loads(row["payload"])
        pnl = _amount(closed.get("net_pnl"))
        if entry.filled_quantity <= 0 or entry.average_price is None or pnl is None:
            continue
        exits = tuple(_order(order, json.loads(order["request"]), json.loads(order["payload"]), sending, fills)
                      for order in orders[row["id"]] if order["kind"] != "entry")
        result.append(LiveTrade(
            id=row["id"], slug=row["slug"], side=request["side"], signal_at=row["signal_at"],
            window_start=row["window_start"], window_end=row["window_end"], entry=entry, exits=exits,
            net_pnl=pnl, fees=_amount(closed.get("fees")), fees_exact=closed.get("fees_exact"),
            payout_total=_amount(closed.get("payout")), exit_reason=closed.get("exit_reason"), closed_at=row["closed_at"],
        ))
    return result


def load_tape_markets(path: Path, trades: list[LiveTrade], *, as_of: float) -> dict[str, TapeMarket]:
    """Load matching market rules and any recorded official payout available at as_of."""
    path = Path(path)
    if not trades or not path.is_file():
        return {}
    markets = {}
    with _readonly(path) as db:
        db.execute("BEGIN")
        for trade in trades:
            row = db.execute("SELECT * FROM markets WHERE slug=? AND start=? AND end=?",
                             (trade.slug, trade.window_start, trade.window_end)).fetchone()
            if row is None:
                continue
            window = json.loads(row["window"])
            if (window.get("asset") != "wti" or window.get("horizon", "15m") != "15m"
                    or (window.get("slug"), window.get("start"), window.get("end"))
                    != (trade.slug, trade.window_start, trade.window_end)):
                continue
            payout, settled_at = _amount(row["payout"]), _stamp(row["settled_at"])
            if payout is None or not ZERO <= payout <= 1 or settled_at is None or not trade.window_end <= settled_at <= as_of:
                payout = settled_at = None
            markets[trade.slug] = TapeMarket(
                slug=trade.slug, window_start=trade.window_start, window_end=trade.window_end,
                price_tick=_amount(window.get("price_tick")), minimum_quantity=_amount(window.get("minimum_quantity")),
                yes_payout=payout, settled_at=settled_at,
            )
    return markets
