"""Versioned Polymarket US fee profiles for simulated fills.

Reviewed schedule: https://docs.polymarket.us/fees, effective 2026-11-14.
Standard taker fee = theta * contracts * price * (1 - price). The paper account
uses the ordinary lowest-volume coefficient (0.095); it does not assume the
temporary new-account coefficient, a better tier, or maker rebates.

Rounding follows the convention already used by ``backend.markets.valuation``:
each fill's exact fee is rounded to the cent, and the order total is capped at
the rounded cumulative exact fee. Recheck the documented rules before relying
on these numbers; a profile's ``verified`` flag records that review.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .common import ONE, ZERO, D, cents, cents_up

TAKER = "taker"
MAKER = "maker"


class FeeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class FeeProfile:
    profile_id: str
    effective_from: datetime
    taker_theta: Decimal
    maker_theta: Decimal | None
    applies_to: tuple
    source: str
    note: str = ""

    def theta(self, role: str) -> Decimal:
        if role == TAKER:
            return self.taker_theta
        if role == MAKER and self.maker_theta is not None:
            return self.maker_theta
        raise FeeUnavailable(f"Fee profile {self.profile_id} has no verified {role} coefficient")


DEFAULT_PROFILES = (
    FeeProfile(
        profile_id="polymarket-us-taker-lowest-volume-2026-11-14",
        effective_from=datetime(2026, 11, 14, tzinfo=timezone.utc),
        taker_theta=Decimal("0.095"),
        # The paper engine only simulates crossing (taker) fills; maker treatment
        # is deliberately left unset so nothing silently prices a fill at zero.
        maker_theta=None,
        applies_to=("moneyline", "total"),
        source="https://docs.polymarket.us/fees (reviewed 2026-11-17)",
        note="Conservative small-account taker profile; no promotional coefficient, no rebates.",
    ),
)


class FeeProvider:
    def __init__(self, profiles=DEFAULT_PROFILES, profile_id: str | None = None):
        self.profiles = tuple(sorted(profiles, key=lambda p: p.effective_from))
        self.profile_id = profile_id

    def profile_at(self, ts: datetime, market_type: str | None = None) -> FeeProfile:
        eligible = [p for p in self.profiles if p.effective_from <= ts
                    and (self.profile_id is None or p.profile_id == self.profile_id)
                    and (market_type is None or market_type in p.applies_to)]
        if not eligible:
            raise FeeUnavailable(f"No fee profile effective at {ts.isoformat()} for {market_type}")
        return eligible[-1]


def exact_fee(theta: Decimal, price: Decimal, quantity: Decimal) -> Decimal:
    price = D(price)
    if not ZERO < price < ONE:
        raise ValueError("Contract price must be strictly between 0 and 1")
    return D(theta) * D(quantity) * price * (ONE - price)


def execution_fee(theta: Decimal, fills) -> Decimal:
    """Fee for one simulated order execution consisting of (price, quantity) fills."""
    exact = [exact_fee(theta, p, q) for p, q in fills if q > 0]
    if not exact:
        return ZERO
    return min(sum((cents(x) for x in exact), ZERO), cents(sum(exact, ZERO)))


def fee_upper_bound(theta: Decimal, price: Decimal, quantity: Decimal) -> Decimal:
    """Reservation-safe bound: ceiling of the exact fee plus a one-cent rounding allowance."""
    return cents_up(exact_fee(theta, price, quantity)) + Decimal("0.01")
