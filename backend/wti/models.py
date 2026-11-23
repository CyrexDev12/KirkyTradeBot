"""Five fair-value models for WTI Up/Down. Every one runs live with its own $100 paper account
under the same entry, sizing, bracket and risk rules, so their results can be compared head
to head and the best one picked.

All settle on a single price at the window end (Chainlink front-month WTI mid), so
P(Up) = P(S_end >= price to beat) with ln(S_end / S_now) modelled over the time left.

| id | name | idea |
|---|---|---|
| A | Random walk | volatility from the last 2 h of 1-minute moves (the gold bot's model) |
| B | Fast volatility | volatility weighted to the last ~10 minutes (EWMA), reacts to busy markets |
| C | Trend follower | adds a drift when the 5- and 20-minute averages agree with the last 15 minutes,
|   |  | carried for at most 15 minutes ahead (so a short trend is not stretched over a daily window) |
| D | Market blend | halfway (in log-odds) between model A and the order book's own price |
| E | News-aware | model B plus the extra move each scheduled release inside the window can add, and
|   |  | higher volatility for a while after a detected price shock |

Checked on WTI 1-minute history (Nov 18–20, 2026; 227 fifteen-minute and 55 hourly windows):
A and B scored a Brier score of 0.15 halfway through a 15-minute window and 0.14 for hourly,
against 0.25 for a coin flip. A plain momentum drift scored worse early in the window, so C
only trends when two averages agree and uses a gentler drift.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from ..updown.model import phi, sigma_per_second

MODEL_INFO = {
    "A": ("Random walk", "Volatility from the last 2 hours; no drift (the gold bot's model)"),
    "B": ("Fast volatility", "Volatility weighted to the last ~10 minutes, so it reacts to busy markets"),
    "C": ("Trend follower", "Leans with the trend when the 5- and 20-minute averages agree"),
    "D": ("Market blend", "Halfway between model A and the order book's price"),
    "E": ("News-aware", "Fast volatility plus the move scheduled releases can add, and extra caution after shocks"),
}


def _closes(minutes):
    return [c for _, c in (minutes or [])]


def ewma_sigma(closes, half_life_min=10.0, min_returns=10, floor=2e-5):
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < min_returns:
        raise ValueError("need more one-minute returns")
    lam = 0.5 ** (1 / half_life_min)
    num = den = 0.0
    for i, r in enumerate(reversed(rets)):
        w = lam ** i
        num += w * r * r
        den += w
    return max(floor, math.sqrt(num / den / 60))


def ema(values, span):
    alpha, out = 2 / (span + 1), values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


def point_probability(ptb, price, tau, sigma, drift=0.0, extra_var=0.0, drift_horizon=None):
    """``drift`` (per second) applies for at most ``drift_horizon`` seconds: a trend measured
    over 15 minutes is not projected across a whole day."""
    if tau <= 0:
        return 1.0 if price >= ptb else 0.0
    sd = math.sqrt(sigma * sigma * tau + extra_var)
    carried = drift * (min(tau, drift_horizon) if drift_horizon else tau)
    return phi((math.log(price / ptb) + carried) / sd)


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


@dataclass
class ModelSet:
    """Builds each model's ``fair_fn`` for the Worker; E reads the shared calendar and detector."""
    calendar: object
    detector: object
    trend_shrink: float = 0.25
    trend_horizon: float = 900.0  # the trend is measured over 15 minutes and carried no further
    shock_boost: float = 1.5  # extra volatility right after a shock (x 1 + boost), decaying
    shock_half_life: float = 600.0

    def fair_fn(self, model_id: str):
        model = getattr(self, f"model_{model_id.lower()}")

        def fn(product, window, now, index_price, sigma, avg, seconds, minutes, book):
            return model(window.price_to_beat, index_price, window.end - now, _closes(minutes), book, now,
                         window.end)
        fn.model_id = model_id
        return fn

    # -- A: random walk, 2-hour volatility ------------------------------------------------
    def model_a(self, ptb, price, tau, closes, book, now, end):
        return point_probability(ptb, price, tau, sigma_per_second(closes[-121:]))

    # -- B: fast EWMA volatility -----------------------------------------------------------
    def model_b(self, ptb, price, tau, closes, book, now, end):
        return point_probability(ptb, price, tau, ewma_sigma(closes[-181:]))

    # -- C: confirmed trend ----------------------------------------------------------------
    def model_c(self, ptb, price, tau, closes, book, now, end):
        recent = closes[-181:]
        sigma = ewma_sigma(recent)
        drift = 0.0
        if len(recent) >= 21:
            fast, slow = ema(recent[-20:], 5), ema(recent[-20:], 20)
            move = math.log(recent[-1] / recent[-16]) / (15 * 60) if len(recent) >= 16 else 0.0
            if (fast - slow) * move > 0:  # both say the same direction
                drift = self.trend_shrink * move
        return point_probability(ptb, price, tau, sigma, drift=drift, drift_horizon=self.trend_horizon)

    # -- D: blend with the market ----------------------------------------------------------
    def model_d(self, ptb, price, tau, closes, book, now, end):
        p = self.model_a(ptb, price, tau, closes, book, now, end)
        if not book or not book.yes_bids or not book.yes_offers:
            return p
        mid = (book.yes_bids[0][0] + book.yes_offers[0][0]) / 2
        return 1 / (1 + math.exp(-(0.5 * _logit(p) + 0.5 * _logit(mid))))

    # -- E: news-aware ---------------------------------------------------------------------
    def model_e(self, ptb, price, tau, closes, book, now, end):
        sigma = ewma_sigma(closes[-181:])
        shock = self.detector.last_shock() if self.detector else None
        if shock is not None and now >= shock:
            sigma *= 1 + self.shock_boost * 0.5 ** ((now - shock) / self.shock_half_life)
        extra = sum(e.size ** 2 for e in self.calendar.between(now, end)) if self.calendar else 0.0
        return point_probability(ptb, price, tau, sigma, extra_var=extra)
