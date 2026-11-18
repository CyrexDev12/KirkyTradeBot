"""Live planning inputs backed by the existing NFL Fair Value modules.

The paper strategy reads the existing artifacts; it does not retrain or alter
them. Market discovery reuses the strict matchers in :mod:`backend.markets` but
fetches through the read-only venue client.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from ..data import DATA, schedule_rows, schedules
from ..markets import match_event, match_total_markets
from ..monte_carlo import total_probabilities
from .common import D, utc
from .valuation import UncertaintyProfile, tie_estimate_from_games

logger = logging.getLogger(__name__)
TRADING_DIR = DATA / "trading"
UNCERTAINTY_PATH = TRADING_DIR / "uncertainty_profiles.json"


def load_uncertainty(path: Path = UNCERTAINTY_PATH) -> dict:
    if not path.exists():
        return {}
    out = {}
    for item in json.loads(path.read_text()).get("profiles", []):
        p = UncertaintyProfile(provider=item["provider"], market_type=item["market_type"],
                               half_width=D(item["half_width"]), method=item["method"],
                               evaluated_games=int(item["evaluated_games"]), source=item["source"])
        out[(p.provider, p.market_type)] = p
    return out


class LiveInputs:
    def __init__(self, venue, store, clock=lambda: datetime.now(timezone.utc)):
        self.venue, self.store, self.clock = venue, store, clock
        self._tie = None
        self._uncertainty = None

    def all_games(self) -> list[dict]:
        return schedule_rows(schedules())

    def games(self, season: int, week: int) -> list[dict]:
        return [g for g in self.all_games() if g["season"] == season and g["week"] == week]

    def gb_prediction(self, game):
        from ..model import get_predictions
        try:
            return get_predictions().get(game["game_id"])
        except RuntimeError:
            return None

    def gb_total_prediction(self, game):
        from ..totals import get_predictions
        try:
            return get_predictions().get(game["game_id"])
        except RuntimeError:
            return None

    def gb_total_over(self, game, line: float):
        from ..totals import over_probability
        pred = self.gb_total_prediction(game)
        return over_probability(pred.get("predicted_total"), line) if pred else None

    def mc_forecast(self, game):
        from ..mc_archive import get_forecasts
        try:
            return get_forecasts(game["season"], [game])[game["game_id"]]
        except Exception as exc:  # unavailable, never substituted
            logger.warning("MC forecast unavailable for %s (%s)", game["game_id"], type(exc).__name__)
            return {"status": "unavailable", "unavailable_reason": f"MC error: {type(exc).__name__}"}

    def mc_total(self, forecast, line: float):
        return total_probabilities(forecast, line) if forecast else None

    def tie_estimate(self):
        if self._tie is None:
            now = self.clock()
            rows = [g for g in self.all_games() if g["game_type"] == "REG" and utc(g["kickoff"]) < now]
            self._tie = tie_estimate_from_games(rows, f"NFLverse regular-season finals before {now.date()}")
        return self._tie

    def uncertainty(self, provider: str, market_type: str):
        if self._uncertainty is None:
            self._uncertainty = load_uncertainty()
        return self._uncertainty.get((provider, market_type))

    async def _events(self) -> list[dict]:
        events = []
        for offset in range(0, 500, 100):
            page = (await self.venue.get("/v2/leagues/nfl/events", {"limit": 100, "offset": offset})).get("events") or []
            events.extend(page)
            if len(page) < 100:
                break
        return events

    async def markets(self, games: list[dict]) -> dict:
        try:
            events = await self._events()
        except Exception as exc:
            logger.warning("NFL events unavailable (%s)", type(exc).__name__)
            return {}
        out = {}
        for game in games:
            ml = match_event(game, events)
            totals = []
            if ml:
                try:
                    detail = (await self.venue.get(f"/v1/events/{ml['event'].get('id')}")).get("event") or {}
                    totals = match_total_markets(game, detail)
                except Exception as exc:
                    logger.warning("Event detail unavailable for %s (%s)", game["game_id"], type(exc).__name__)
            out[game["game_id"]] = {"moneyline": ml, "totals": totals}
        return out

    async def book(self, slug: str):
        try:
            snap = await self.venue.book(slug)
        except Exception as exc:
            logger.warning("Book unavailable for %s (%s)", slug, type(exc).__name__)
            return None
        self.store.record_snapshot(snap)
        return snap

    def ready_for(self, season: int, week: int, now: datetime) -> tuple[bool, str | None]:
        """Model-based plans wait until the GB artifact covers the week and was refreshed after the slate."""
        from ..model import PREDICTIONS
        games = self.games(season, week)
        if not games:
            return False, "no scheduled games"
        if not PREDICTIONS.exists():
            return False, "GB predictions not built"
        preds = json.loads(PREDICTIONS.read_text())
        missing = [g["game_id"] for g in games if g["game_id"] not in preds]
        if missing:
            return False, f"GB predictions missing for {len(missing)} games"
        prev_final = max((utc(g["kickoff"]) for g in self.all_games()
                          if g["home_score"] is not None and utc(g["kickoff"]) < now), default=None)
        refreshed = datetime.fromtimestamp(PREDICTIONS.stat().st_mtime, timezone.utc)
        if prev_final and refreshed < prev_final:
            return False, "GB predictions not refreshed since the previous slate's results"
        return True, None
