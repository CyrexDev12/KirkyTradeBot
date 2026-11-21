"""Deterministic trend screen and risk budget for one BTC paper position."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
from statistics import mean
from zoneinfo import ZoneInfo

from .market import Bar, Quote

PHOENIX = ZoneInfo("America/Phoenix")
ENTRY_GRACE = timedelta(seconds=60)
FEE_RATE = 0.004  # Simulation assumption per side, independent of exchange fee tiers.
SLIPPAGE = 0.0002  # Additional adverse fill movement beyond crossing the observed spread.


def current_slot(now: datetime) -> datetime | None:
    local = now.astimezone(PHOENIX)
    if not 6 <= local.hour < 9:
        return None
    return local.replace(minute=local.minute // 30 * 30, second=0, microsecond=0).astimezone(now.tzinfo)


@dataclass(frozen=True)
class Signal:
    direction: str | None
    reason: str
    confidence: float = 0.0  # Trend-quality score, not a calibrated probability.
    volatility: float = 0.0  # Mean true range over 14 completed 15-minute bars / latest close.
    indicators: dict = field(default_factory=dict)


def ema(values, period):
    smoothed = values[0]
    for value in values[1:]:
        smoothed += 2 / (period + 1) * (value - smoothed)
    return smoothed


def trend(bars: list[Bar]) -> Signal:
    # Aggregating closed five-minute buckets keeps direction and confirmation
    # on the same Coinbase observations and excludes the currently forming bar.
    fifteen = []
    for start in range(0, len(bars), 3):
        group = bars[start:start + 3]
        if len(group) != 3 or group[0].start % 900:
            raise ValueError("unaligned fifteen-minute series")
        fifteen.append(Bar(group[0].start, min(b.low for b in group), max(b.high for b in group),
                           group[0].open, group[-1].close))
    closes = [b.close for b in fifteen]
    span = mean(max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
                for p, b in zip(fifteen[-15:-1], fifteen[-14:]))
    volatility = span / closes[-1]
    if not 0.0005 <= volatility <= 0.025:
        return Signal(None, "15-minute volatility outside strategy range", volatility=volatility)
    spread = ema(closes, 5) - ema(closes, 18)
    momentum = closes[-1] - closes[-4]
    confirmation = bars[-1].close - bars[-2].close
    path = sum(abs(b - a) for a, b in zip(closes[-9:-1], closes[-8:]))
    efficiency = abs(closes[-1] - closes[-9]) / path if path else 0.0
    indicators = {"ema_gap_atr": round(spread / span, 4), "momentum_atr": round(momentum / span, 4),
                  "efficiency": round(efficiency, 4), "last_close": closes[-1]}
    if spread * momentum <= 0 or spread * confirmation <= 0 or abs(spread) < 0.35 * span or efficiency < 0.4:
        return Signal(None, "5-minute confirmation or 15-minute trend too weak", volatility=volatility,
                      indicators=indicators)
    quality = min(1.0, abs(spread) / span) * efficiency
    return Signal("UP" if spread > 0 else "DOWN", "15-minute trend confirmed by 5-minute momentum",
                  quality, volatility, indicators)


def paper_entry(signal: Signal, quote: Quote, balance: float, risk_multiplier=1.0):
    """Size below both risk and collateral ceilings, including simulated exit costs."""
    mid = (quote.ask + quote.bid) / 2
    if (quote.ask - quote.bid) / mid > 0.0008:
        return None, "bid/ask spread too wide"
    if abs(mid / signal.indicators["last_close"] - 1) > max(0.0025, signal.volatility):
        return None, "price moved away from the signal"
    sign = 1 if signal.direction == "UP" else -1
    entry = quote.ask * (1 + SLIPPAGE) if sign == 1 else quote.bid * (1 - SLIPPAGE)
    stop_fraction = max(0.0015, signal.volatility)
    stop = entry * (1 - sign * stop_fraction)
    # A 1.5:1 net reward target accounts for fees on entry and exit.
    stop_fill = stop * (1 - sign * SLIPPAGE)
    risk_per_btc = abs(entry - stop_fill) + FEE_RATE * (entry + stop_fill)
    target_fill = (entry * (sign + FEE_RATE) + 1.5 * risk_per_btc) / (sign - FEE_RATE)
    target = target_fill / (1 - sign * SLIPPAGE)
    # Heuristic ceiling for a single 15-minute hold. Even a strong directional
    # trend cannot justify a fee-adjusted exit many normal bars away.
    if abs(target - mid) > 3 * signal.volatility * mid:
        return None, "fee-adjusted target exceeds three recent 15-minute average ranges"
    risk_budget = balance * 0.005 * signal.confidence
    notional_cap = balance * 0.20 * signal.confidence
    size = risk_multiplier * min(risk_budget / risk_per_btc, notional_cap / (entry * (1 + FEE_RATE)))
    size = math.floor(size * 1e8) / 1e8
    if size <= 0 or size * entry < 1:
        return None, "risk budget too small"
    return {"direction": signal.direction, "entry_price": entry, "quantity": size,
            "stop_loss": stop, "take_profit": target, "entry_fee": entry * size * FEE_RATE,
            "risk_at_stop": size * risk_per_btc,
            "target_net_profit": size * (sign * (target_fill - entry) - FEE_RATE * (entry + target_fill)),
            "confidence": signal.confidence,
            "volatility": signal.volatility, "fee_rate": FEE_RATE, "slippage": SLIPPAGE,
            "indicators": signal.indicators}, None


def paper_exit(trade: dict, quote: Quote, now: datetime):
    up = trade["direction"] == "UP"
    observed = quote.bid if up else quote.ask
    sign = 1 if up else -1
    if sign * (observed - trade["stop_loss"]) <= 0:
        reason = "stop_loss"
    elif sign * (observed - trade["take_profit"]) >= 0:
        reason = "take_profit"
    elif min(now, quote.at) >= datetime.fromisoformat(trade["expires_at"]):
        reason = "window_end"
    else:
        return None
    fill = observed * (1 - sign * trade["slippage"])
    qty = trade["quantity"]
    fee = fill * qty * trade["fee_rate"]
    pnl = sign * (fill - trade["entry_price"]) * qty - trade["entry_fee"] - fee
    return {"status": "CLOSED", "closed_at": now.isoformat(), "exit_reason": reason,
            "exit_price": fill, "exit_fee": fee, "net_pnl": round(pnl, 8), "exit_quote_at": quote.at.isoformat()}
