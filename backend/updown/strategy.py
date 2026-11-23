"""Entry, early exit and sizing rules for Up/Down paper bets.

The same engine runs two desks: crypto 15-minute windows (prefix ``UPDOWN_``)
and gold 5-minute/15-minute/hourly windows (prefix ``GOLD_``, see
``backend/gold``). Settings come from environment variables (crypto defaults in
brackets; gold uses the same names with ``GOLD_``):

* ``UPDOWN_ASSETS`` [btc,eth,sol] / ``GOLD_HORIZONS`` [5m,15m,1h]
* ``UPDOWN_MIN_EDGE`` [0.02] — fair value minus price minus fee, per $1 contract
* ``UPDOWN_PRICE_RANGE`` [0.10-0.90] — skip contracts priced outside this range
* ``UPDOWN_KELLY_FRACTION`` [0.25] — share of the Kelly stake to use
* ``UPDOWN_MAX_STAKE`` [0.05] — max stake (= max loss) per bet, share of balance
* ``UPDOWN_MAX_OPEN_STAKE`` [0.15] — max total stake across open bets
* ``UPDOWN_DAILY_LOSS_LIMIT`` [0.10] — no new bets after this realized loss in an Arizona day
* ``UPDOWN_TAKE_PROFIT_EDGE`` [0.01] — sell early when the bid beats fair value by this much after fees
* ``UPDOWN_TAKE_PROFIT`` [off; gold 0.5] — take-profit target: sell once the bid has captured this share
  of the room between the entry price and $1 (0.5 on a 52¢ entry = 76¢)
* ``UPDOWN_STOP_LOSS`` [off; gold 0.5] — stop-loss: sell once the bid has fallen this share of the
  entry price (0.5 on a 52¢ entry = 26¢)
* ``UPDOWN_COINBASE_MAX_GAP`` [0.002] — skip when Coinbase and the settlement index disagree by more
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os

from .model import fair_value, taker_fee

HORIZON_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


@dataclass(frozen=True)
class Product:
    """One Polymarket Up/Down series, e.g. btc 15m (60-second average) or gold 1h (point price)."""
    symbol: str
    asset_class: str = "crypto"  # history id prefix: crypto:btc, commodity:gold
    horizon: str = "15m"
    rule: str = "avg60"  # "avg60" (CF Benchmarks crypto) | "point" (Chainlink gold)
    coinbase: str | None = None  # cross-check product, e.g. BTC-USD or PAXG-USD

    @property
    def seconds(self) -> int:
        return HORIZON_SECONDS[self.horizon]

    @property
    def key(self) -> str:
        return f"{self.symbol}-{self.horizon}"

    @property
    def index_id(self) -> str:
        return f"{self.asset_class}:{self.symbol}"


def crypto_products(assets) -> tuple:
    return tuple(Product(a, "crypto", "15m", "avg60", f"{a.upper()}-USD") for a in assets)


def gold_products(horizons) -> tuple:
    return tuple(Product("gold", "commodity", h, "point", "PAXG-USD") for h in horizons)


@dataclass(frozen=True)
class Settings:
    products: tuple = crypto_products(("btc", "eth", "sol"))
    min_edge: float = 0.02
    price_min: float = 0.10
    price_max: float = 0.90
    entry_after_seconds: int = 60
    entry_before_end_seconds: int = 90
    kelly_fraction: float = 0.25
    max_stake: float = 0.05
    max_open_stake: float = 0.15
    daily_loss_limit: float = 0.10
    take_profit_edge: float = 0.01
    coinbase_max_gap: float = 0.002
    participation: float = 0.5
    default_theta: float = 0.095
    max_index_age: float = 10.0  # seconds; older settlement-index prices are treated as stale
    take_profit_target: float = 0.0  # share of (1 - entry) to capture before selling; 0 = hold to settlement
    stop_loss: float = 0.0  # share of the entry price to lose before selling; 0 = no stop

    @property
    def assets(self) -> tuple:
        return tuple(dict.fromkeys(p.symbol for p in self.products))

    @classmethod
    def from_env(cls, env=None, prefix="UPDOWN") -> "Settings":
        env = os.environ if env is None else env
        gold = prefix == "GOLD"

        def get(name, default):
            return env.get(f"{prefix}_{name}", default)

        lo, hi = (float(x) for x in get("PRICE_RANGE", "0.10-0.90").split("-", 1))
        if gold:
            products = gold_products(h.strip() for h in get("HORIZONS", "5m,15m,1h").split(",") if h.strip())
        else:
            products = crypto_products(a.strip().lower() for a in get("ASSETS", "btc,eth,sol").split(",") if a.strip())
        s = cls(products=products, min_edge=float(get("MIN_EDGE", 0.02)), price_min=lo, price_max=hi,
                kelly_fraction=float(get("KELLY_FRACTION", 0.25)), max_stake=float(get("MAX_STAKE", 0.05)),
                max_open_stake=float(get("MAX_OPEN_STAKE", 0.15)),
                daily_loss_limit=float(get("DAILY_LOSS_LIMIT", 0.10)),
                take_profit_edge=float(get("TAKE_PROFIT_EDGE", 0.01)),
                # PAXG (tokenized gold) trades at a small premium to spot, so gold allows a wider gap.
                coinbase_max_gap=float(get("COINBASE_MAX_GAP", 0.01 if gold else 0.002)),
                max_index_age=float(get("MAX_INDEX_AGE", 30 if gold else 10)),
                take_profit_target=float(get("TAKE_PROFIT", 0.5 if gold else 0)),
                stop_loss=float(get("STOP_LOSS", 0.5 if gold else 0)))
        if not (0 < s.price_min < s.price_max < 1 and 0 < s.max_stake <= s.max_open_stake <= 1):
            raise ValueError("Invalid Up/Down settings")
        if not (0 <= s.take_profit_target < 1 and 0 <= s.stop_loss < 1):
            raise ValueError("Take-profit and stop-loss must be between 0 and 1")
        if any(p.horizon not in HORIZON_SECONDS for p in s.products):
            raise ValueError("Up/Down horizons must be 5m, 15m or 1h")
        return s


SETTINGS = Settings.from_env()


@dataclass(frozen=True)
class Window:
    asset: str
    slug: str  # market slug
    start: int  # unix seconds
    end: int
    price_to_beat: float | None
    status: str
    theta: float
    horizon: str = "15m"
    rule: str = "avg60"

    @property
    def key(self) -> str:
        return f"{self.asset}-{self.horizon}"


@dataclass(frozen=True)
class Book:
    yes_bids: tuple  # ((price, qty), ...) best first
    yes_offers: tuple
    at: float  # unix seconds the book was read

    def side_asks(self, side: str):
        """Prices to BUY the side, best first."""
        if side == "UP":
            return self.yes_offers
        return tuple((round(1 - p, 6), q) for p, q in self.yes_bids)

    def side_bids(self, side: str):
        """Prices to SELL the side, best first."""
        if side == "UP":
            return self.yes_bids
        return tuple((round(1 - p, 6), q) for p, q in self.yes_offers)


def bracket_prices(entry: float, settings: Settings):
    """(take-profit price, stop-loss price) on the 1¢ tick; None where switched off."""
    tp = sl = None
    if settings.take_profit_target:
        tp = min(0.99, math.ceil((entry + settings.take_profit_target * (1 - entry)) * 100 - 1e-9) / 100)
    if settings.stop_loss:
        sl = max(0.01, math.floor(entry * (1 - settings.stop_loss) * 100 + 1e-9) / 100)
    return tp, sl


def bracket_metrics(entry: float, qty: int, cost: float, theta: float, settings: Settings) -> dict:
    """What the ticket states: exit prices and the P&L if each one fills (after the exit fee)."""
    tp, sl = bracket_prices(entry, settings)
    out = {"take_profit_price": tp, "stop_loss_price": sl, "profit_at_take_profit": None, "loss_at_stop": None,
           "reward_to_risk": None}
    if tp is not None:
        out["profit_at_take_profit"] = round(qty * (tp - taker_fee(tp, theta)) - cost, 4)
    if sl is not None:
        out["loss_at_stop"] = round(qty * (sl - taker_fee(sl, theta)) - cost, 4)
    if tp is not None and sl is not None and out["loss_at_stop"] < 0:
        out["reward_to_risk"] = round(out["profit_at_take_profit"] / -out["loss_at_stop"], 2)
    return out


@dataclass
class Decision:
    action: str  # "BUY" | "SKIP"
    reason: str
    side: str | None = None
    fair_up: float | None = None
    fair_side: float | None = None
    price: float | None = None  # average fill price per contract
    quantity: int = 0
    fee: float = 0.0
    cost: float = 0.0  # stake = price paid + fee = maximum loss
    edge: float | None = None  # per contract, after fees
    risk: dict = field(default_factory=dict)


def evaluate(window: Window, book: Book, now: float, index_price: float, sigma: float,
             closing_avg: float | None, coinbase_price: float | None, balance: float, open_stake: float,
             today_pnl: float, held: bool, settings: Settings = SETTINGS) -> Decision:
    s = settings
    if held:
        return Decision("SKIP", "already holding this window")
    if window.status != "MARKET_STATUS_OPEN":
        return Decision("SKIP", f"market not open ({window.status})")
    if window.price_to_beat is None:
        return Decision("SKIP", "price to beat not published yet")
    p_up = fair_value(window.rule, window.price_to_beat, index_price, window.end - now, sigma, closing_avg)
    if not window.start + s.entry_after_seconds <= now <= window.end - s.entry_before_end_seconds:
        return Decision("SKIP", "outside the entry part of the window", fair_up=p_up)
    if today_pnl <= -s.daily_loss_limit * (balance - today_pnl):
        return Decision("SKIP", "daily loss limit reached", fair_up=p_up)
    if coinbase_price is None:
        return Decision("SKIP", "no fresh Coinbase price", fair_up=p_up)
    if abs(coinbase_price / index_price - 1) > s.coinbase_max_gap:
        return Decision("SKIP", "Coinbase and the settlement index disagree", fair_up=p_up)
    best = None
    for side, fair in (("UP", p_up), ("DOWN", 1 - p_up)):
        asks = book.side_asks(side)
        if not asks:
            continue
        top = asks[0][0]
        if not s.price_min <= top <= s.price_max:
            continue
        edge = fair - top - taker_fee(top, window.theta)
        if best is None or edge > best[2]:
            best = (side, fair, edge, asks)
    if best is None:
        return Decision("SKIP", "both sides priced outside the tradable range", fair_up=p_up)
    side, fair, edge, asks = best
    if edge < s.min_edge:
        return Decision("SKIP", f"best edge {edge * 100:.1f}c below {s.min_edge * 100:.1f}c", side=side,
                        fair_up=p_up, fair_side=fair, price=asks[0][0], edge=edge)
    top = asks[0][0]
    unit = top + taker_fee(top, window.theta)
    kelly = max(0.0, (fair - unit) / (1 - unit))
    budget = min(balance * s.max_stake, balance * s.kelly_fraction * kelly,
                 balance * s.max_open_stake - open_stake, balance - open_stake)
    qty, cost, fills = 0, 0.0, []
    for price, displayed in asks:
        if not s.price_min <= price <= s.price_max:
            break
        level_unit = price + taker_fee(price, window.theta)
        if fair - level_unit < s.min_edge:
            break
        room = int(displayed * s.participation)
        take = min(room, int((budget - cost) // level_unit))
        if take <= 0:
            continue
        fills.append((price, take))
        qty += take
        cost += take * level_unit
    if qty < 1:
        return Decision("SKIP", "stake too small for one contract at the allowed risk", side=side, fair_up=p_up,
                        fair_side=fair, price=top, edge=edge)
    gross = sum(p * q for p, q in fills)
    fee = sum(taker_fee(p, window.theta) * q for p, q in fills)
    avg = gross / qty
    risk = {"max_loss": round(gross + fee, 4), "max_loss_pct": (gross + fee) / balance,
            "payout_if_right": float(qty), "profit_if_right": round(qty - gross - fee, 4),
            "expected_profit": round(qty * fair - gross - fee, 4), "open_stake_after": open_stake + gross + fee,
            "kelly_fraction": s.kelly_fraction, "max_stake_pct": s.max_stake,
            "daily_loss_limit": s.daily_loss_limit, **bracket_metrics(avg, qty, gross + fee, window.theta, s)}
    return Decision("BUY", f"{side} fair {fair:.1%} vs {avg * 100:.0f}c ask", side=side, fair_up=p_up,
                    fair_side=fair, price=avg, quantity=qty, fee=fee, cost=gross + fee,
                    edge=(qty * fair - gross - fee) / qty, risk=risk)


def early_exit(side: str, quantity: int, book: Book, fair_up_now: float, theta: float,
               settings: Settings = SETTINGS):
    """Sell when the market bids more than the contract is worth (after the exit fee)."""
    fair = fair_up_now if side == "UP" else 1 - fair_up_now
    sold, proceeds, fee = 0, 0.0, 0.0
    for price, displayed in book.side_bids(side):
        if price - taker_fee(price, theta) < fair + settings.take_profit_edge:
            break
        take = min(quantity - sold, int(displayed * settings.participation))
        if take <= 0:
            continue
        sold += take
        proceeds += price * take
        fee += taker_fee(price, theta) * take
        if sold >= quantity:
            break
    if sold <= 0:
        return None
    return {"quantity": sold, "price": proceeds / sold, "gross": proceeds, "fee": fee, "fair_side": fair}


def bracket_exit(trade: dict, book: Book, settings: Settings = SETTINGS):
    """Act on the ticket's take-profit / stop-loss. Returns a sale like ``early_exit`` with ``kind``
    "target" or "stop_loss", or None. A stop sells into whatever bids are there (it can fill below
    the stop price if the book is thin); a target only sells at or above its price."""
    side, remaining, theta = trade["side"], trade["remaining"], trade["theta"]
    tp, sl = trade.get("take_profit_price"), trade.get("stop_loss_price")
    bids = book.side_bids(side)
    if remaining <= 0 or not bids or (tp is None and sl is None):
        return None
    best = bids[0][0]
    if tp is not None and best >= tp - 1e-9:
        kind, floor = "target", tp
    elif sl is not None and best <= sl + 1e-9:
        kind, floor = "stop_loss", 0.0
    else:
        return None
    sold, proceeds, fee = 0, 0.0, 0.0
    for price, displayed in bids:
        if price < floor - 1e-9:
            break
        take = min(remaining - sold, int(displayed * settings.participation))
        if take <= 0:
            continue
        sold += take
        proceeds += price * take
        fee += taker_fee(price, theta) * take
        if sold >= remaining:
            break
    if sold <= 0:
        return None
    return {"kind": kind, "quantity": sold, "price": proceeds / sold, "gross": proceeds, "fee": fee,
            "fair_side": None, "trigger": tp if kind == "target" else sl}
