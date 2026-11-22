"""Fair value of a 15-minute Up/Down contract.

Settlement rule (Polymarket US crypto Up/Down, reviewed 2026-11-22): the open
("price to beat") and the close are each the simple average of the 60 one-second
BRTI prices in the minute before the window start / end. Up pays $1 when
close >= open (a flat window settles Up).

Model: log price follows a driftless random walk with per-second volatility
sigma. With tau seconds left:

* tau > 60: the close averages a future minute, so
  ln(close / now) ~ N(0, sigma^2 * ((tau - 60) + 60/3))
* tau <= 60: (60 - tau) seconds of the closing minute are already known
  (average A); the remaining tau seconds average to F with
  ln(F / now) ~ N(0, sigma^2 * tau / 3); Up needs F >= (60*open - (60-tau)*A) / tau.

Checked against 2.5 days of BRTI history for BTC/ETH/SOL (2026-11-20..22): Brier
0.17-0.19 at minute 5 versus 0.25 for a coin flip; the 10-20% band was
overconfident for BTC, so the strategy avoids contracts priced under 10c or over 90c.
"""
from __future__ import annotations

import math


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sigma_per_second(minute_closes: list[float], floor: float = 2e-5) -> float:
    """Per-second volatility from consecutive one-minute index closes."""
    rets = [math.log(b / a) for a, b in zip(minute_closes, minute_closes[1:]) if a > 0 and b > 0]
    if len(rets) < 30:
        raise ValueError("need at least 30 one-minute returns")
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return max(floor, math.sqrt(var / 60.0))


def fair_up(price_to_beat: float, now_price: float, seconds_left: float, sigma: float,
            closing_minute_avg: float | None = None) -> float:
    """Probability that the window settles Up."""
    if price_to_beat <= 0 or now_price <= 0 or sigma <= 0:
        raise ValueError("prices and sigma must be positive")
    tau = max(0.0, float(seconds_left))
    if tau > 60:
        sd = sigma * math.sqrt((tau - 60) + 20)
        return phi(math.log(now_price / price_to_beat) / sd)
    known = 60 - tau
    if known > 0 and closing_minute_avg is None:
        raise ValueError("closing-minute average needed inside the final minute")
    if tau <= 0:
        return 1.0 if (closing_minute_avg or now_price) >= price_to_beat else 0.0
    needed = (60 * price_to_beat - known * (closing_minute_avg or 0.0)) / tau
    if needed <= 0:
        return 1.0
    sd = sigma * math.sqrt(tau / 3)
    return phi(math.log(now_price / needed) / sd)


def taker_fee(price: float, theta: float) -> float:
    """Polymarket US taker fee per contract: theta * p * (1 - p)."""
    return theta * price * (1 - price)
