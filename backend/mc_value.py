"""Map paired score outcomes to the payouts of previously matched contracts."""
from .markets import valuation
from .monte_carlo import MODEL_ID, total_probabilities


def _value(payout, quote, quantity):
    value = valuation(payout, quote, quantity)
    if value is None:
        return None
    value.pop("category", None)
    return {**value, "model_id": MODEL_ID, "fair_value": payout,
            "fee_coefficient": quote.get("fee_coefficient"), "fee_source": "market.feeCoefficient"}


def market_values(forecast, market, totals, quantity):
    values = dict(model_id=MODEL_ID, moneyline={}, totals=[])
    if forecast["status"] != "available":
        return values
    tie_payout = (market or {}).get("tie_payout")
    if tie_payout is not None:
        for role in ("home", "away"):
            team = forecast[role]
            payout = forecast[f"probability_{role}_win"] + tie_payout * forecast["probability_tie"]
            values["moneyline"][team] = _value(payout, market["outcomes"].get(team), quantity)
    for quote in (totals or {}).get("lines", []):
        probs = total_probabilities(forecast, quote["line"])
        if probs is None:
            continue
        entry = dict(line=quote["line"], status=quote["status"], probabilities=probs)
        for side in ("over", "under"):
            payout = probs[side] if quote.get("full_game_half_point_payout") else None
            entry[side] = _value(payout, quote["outcomes"].get(side), quantity)
        values["totals"].append(entry)
    return values
