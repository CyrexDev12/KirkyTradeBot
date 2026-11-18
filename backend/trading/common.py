"""Decimal money helpers and time parsing shared by the paper-trading modules."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
import hashlib
import json

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


def D(value) -> Decimal:
    """Exact decimal from int/str/Decimal; floats go through ``str`` to avoid binary artefacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def cents(value: Decimal) -> Decimal:
    return D(value).quantize(CENT, ROUND_HALF_UP)


def cents_up(value: Decimal) -> Decimal:
    return D(value).quantize(CENT, ROUND_CEILING)


def floor_step(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("Quantity step must be positive")
    return (D(quantity) / step).to_integral_value(ROUND_FLOOR) * step


def on_tick(price: Decimal, tick: Decimal) -> bool:
    return tick > 0 and (D(price) / tick) == (D(price) / tick).to_integral_value()


def utc(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def dumps(value) -> str:
    return json.dumps(value, default=_default, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()[:16]
