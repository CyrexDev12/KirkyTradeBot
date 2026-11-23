"""Bound the influence of extreme historical observations without deleting games."""
from __future__ import annotations

import numpy as np


def median_scale(values, min_scale: float) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    center = float(np.median(values))
    # A normal sample's median absolute deviation is about 0.6745 of its standard deviation.
    scale = max(min_scale, 1.4826 * float(np.median(np.abs(values - center))))
    return center, scale


def winsorize(values, *, min_scale: float = 0.0, min_samples: int = 6,
              threshold: float = 3.0, lower: float | None = None,
              upper: float | None = None) -> np.ndarray:
    """Use a median/MAD fence from the supplied finite, pregame observations only."""
    values = np.asarray(values, dtype=float)
    if len(values) < min_samples:
        return values
    center, scale = median_scale(values, min_scale)
    if scale == 0:
        return values
    lo, hi = center - threshold * scale, center + threshold * scale
    return np.clip(values, max(lo, lower) if lower is not None else lo,
                   min(hi, upper) if upper is not None else hi)


def point_history(values) -> np.ndarray:
    """Limit isolated team points extremes using at least six games and an 8-point scale floor."""
    return winsorize(values, min_scale=8.0, lower=0.0)
