"""Reference prices for the same selections and lines published in the NFLverse schedule."""
from __future__ import annotations

import math

import polars as pl

from ..data import canonical


def _reference_price(american):
    # The schedule uses signed integer American prices. Nulls and placeholders
    # must remain missing, including when the other side of the market is priced.
    if (isinstance(american, bool) or not isinstance(american, (int, float))
            or not math.isfinite(american) or not float(american).is_integer() or abs(american) < 100):
        return None
    decimal = 1 + (american / 100 if american > 0 else 100 / abs(american))
    return {"american": int(american), "decimal": decimal, "source": "NFLverse schedule"}


def schedule_prices(schedule: pl.DataFrame, game_ids) -> dict:
    """Index cached schedule odds by (game_id, market, selection, line).

    NFLverse supplies side-specific odds but neither a bookmaker ID nor a
    quote timestamp. This lookup never substitutes odds from another line.
    """
    ids = list(game_ids)
    if not ids or "game_id" not in schedule.columns:
        return {}
    prices = {}
    rows = schedule.filter(pl.col("game_id").is_in(ids))
    for row in rows.iter_rows(named=True):
        if row.get("home_score") is not None or row.get("away_score") is not None:
            continue
        gid = row["game_id"]
        for side in ("home", "away"):
            team = canonical(row.get(f"{side}_team"))
            price = _reference_price(row.get(f"{side}_moneyline"))
            if team and price:
                prices[gid, "moneyline", team, None] = price

        line = row.get("total_line")
        if not isinstance(line, (int, float)) or not math.isfinite(line) or line <= 0 or line % 1 != 0.5:
            continue
        for side in ("Over", "Under"):
            price = _reference_price(row.get(f"{side.lower()}_odds"))
            if price:
                prices[gid, "total", side, line] = price
    return prices
