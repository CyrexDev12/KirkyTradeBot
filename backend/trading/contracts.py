"""Venue contract mapping, outcome orientation and payout-correct model values.

Mapping reuses the strict matchers in :mod:`backend.markets` and adds the
fields the paper engine needs. Nothing infers a side from array position or a
display slug: orientation comes from each market side's ``long`` flag and its
structured team/description metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re

from ..data import canonical
from .book import NO, YES
from .common import D, utc

SETTLEMENT_RULE_MONEYLINE = "moneyline-full-game-ot-tie-0.50-v1"
SETTLEMENT_RULE_TOTAL = "total-full-game-ot-half-point-v1"

# Field names checked for venue increments. These must be confirmed against
# captured responses; a market whose increments are unknown cannot qualify.
TICK_FIELDS = ("tickSize", "minimumTickSize", "orderPriceMinTickSize", "priceIncrement")
QTY_STEP_FIELDS = ("quantityIncrement", "qtyIncrement", "lotSize")
MIN_QTY_FIELDS = ("minimumTradeQty", "minimumQuantity", "minQty", "minOrderSize")


class MappingError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ContractSpec:
    venue: str
    market_slug: str
    market_id: str | None
    event_id: str | None
    game_id: str
    season: int
    week: int
    home: str
    away: str
    kickoff: datetime
    market_type: str  # "moneyline" | "total"
    selected_outcome: str  # team abbreviation, "over" or "under"
    selected_side: str  # YES (long side of canonical instrument) or NO
    total_line: Decimal | None
    tie_payout: Decimal | None
    settlement_rule_version: str
    tick_size: Decimal | None
    quantity_step: Decimal | None
    min_quantity: Decimal | None

    @property
    def increments_verified(self) -> bool:
        return self.tick_size is not None and self.quantity_step is not None

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, datetime) else v)
                for k, v in self.__dict__.items()}


def _first_decimal(market: dict, keys) -> Decimal | None:
    for key in keys:
        value = market.get(key)
        if isinstance(value, dict):
            value = value.get("value")
        if value is not None:
            try:
                result = D(value)
            except Exception:
                continue
            if result > 0:
                return result
    return None


def _quantities(market: dict) -> tuple[Decimal | None, Decimal | None]:
    """Quantity step and minimum.

    Polymarket US NFL markets (observed 2026-11-17) publish ``minimumTradeQty``
    (0.01) but no explicit increment field. Whole contracts are always valid
    multiples of a fractional minimum, so the paper account trades whole
    contracts in that case instead of assuming a fractional step.
    """
    step = _first_decimal(market, QTY_STEP_FIELDS)
    minimum = _first_decimal(market, MIN_QTY_FIELDS)
    if step is None and minimum is not None and minimum <= 1 and (Decimal(1) / minimum) % 1 == 0:
        return Decimal(1), Decimal(1)
    return step, minimum


def _base(game: dict, market: dict, event: dict) -> dict:
    step, minimum = _quantities(market)
    return dict(venue="polymarket_us", market_slug=market["slug"], market_id=market.get("id"),
                event_id=str(event.get("id")) if event.get("id") is not None else None,
                game_id=game["game_id"], season=int(game["season"]), week=int(game["week"]),
                home=game["home"], away=game["away"], kickoff=utc(game["kickoff"]),
                tick_size=_first_decimal(market, TICK_FIELDS), quantity_step=step, min_quantity=minimum)


def moneyline_spec(game: dict, match: dict | None, team: str) -> ContractSpec:
    """``match`` is the output of :func:`backend.markets.match_event`."""
    if not match:
        raise MappingError("NO_LISTED_MARKET", "No verified full-game winner market")
    team = canonical(team)
    if team not in (game["home"], game["away"]):
        raise MappingError("CONTRACT_MAPPING_FAILED", f"{team} is not a team in {game['game_id']}")
    side = (match.get("sides") or {}).get(team)
    if side is None or canonical((side.get("team") or {}).get("abbreviation")) != team:
        raise MappingError("CONTRACT_MAPPING_FAILED", f"No structured market side for {team}")
    expected_role = "home" if team == game["home"] else "away"
    if (side.get("team") or {}).get("ordering") != expected_role:
        raise MappingError("CONTRACT_MAPPING_FAILED", f"{team} side role does not match the schedule")
    market = match["market"]
    from ..markets import _tie_payout  # verified fixed-payout clauses only
    tie = _tie_payout(market)
    if tie is None:
        raise MappingError("UNSUPPORTED_SETTLEMENT", "Tie settlement clause is not a recognized $0.50 rule")
    return ContractSpec(**_base(game, market, match["event"]), market_type="moneyline", selected_outcome=team,
                        selected_side=YES if side.get("long") else NO, total_line=None,
                        tie_payout=D(str(tie)), settlement_rule_version=SETTLEMENT_RULE_MONEYLINE)


def total_spec(game: dict, match: dict | None, outcome: str) -> ContractSpec:
    """``match`` is one element of :func:`backend.markets.match_total_markets`."""
    if not match:
        raise MappingError("NO_LISTED_MARKET", "No verified full-game combined total market")
    outcome = outcome.lower()
    if outcome not in ("over", "under"):
        raise MappingError("CONTRACT_MAPPING_FAILED", "Total outcome must be over or under")
    line = D(str(match["line"]))
    if line % 1 != Decimal("0.5"):
        raise MappingError("UNSUPPORTED_SETTLEMENT", "Only half-point lines are supported (no push handling)")
    market = match["market"]
    rule = re.search(r"\bcombine for over (\d+\.\d+) points\b", (market.get("description") or "").lower())
    if not rule or D(rule[1]) != line:
        raise MappingError("UNSUPPORTED_SETTLEMENT", "Description line does not match the market line")
    side = match["sides"][outcome]
    expected_long = outcome == "over"
    if bool(side.get("long")) != expected_long:
        raise MappingError("CONTRACT_MAPPING_FAILED", "Over must be the long side and Under the short side")
    return ContractSpec(**_base(game, market, match["event"]), market_type="total", selected_outcome=outcome,
                        selected_side=YES if expected_long else NO, total_line=line, tie_payout=None,
                        settlement_rule_version=SETTLEMENT_RULE_TOTAL)


def settlement_payout(spec: ContractSpec, home_score: int, away_score: int) -> Decimal:
    """Selected-outcome payout per contract from verified final scores (overtime included)."""
    if spec.market_type == "moneyline":
        if home_score == away_score:
            if spec.tie_payout is None:
                raise MappingError("UNSUPPORTED_SETTLEMENT", "Tie payout unknown")
            return spec.tie_payout
        winner = spec.home if home_score > away_score else spec.away
        return Decimal("1") if winner == spec.selected_outcome else Decimal("0")
    total = D(home_score + away_score)
    over = total > spec.total_line
    return Decimal("1") if over == (spec.selected_outcome == "over") else Decimal("0")
