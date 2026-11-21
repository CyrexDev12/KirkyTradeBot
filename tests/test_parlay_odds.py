import polars as pl
import pytest

from backend.trading.parlay_odds import schedule_prices


def test_schedule_prices_match_exact_team_side_and_total():
    active = {"game_id": "week11", "home_team": "LAR", "away_team": "SF", "home_moneyline": 150,
              "away_moneyline": -200, "total_line": 43.5, "over_odds": -120, "under_odds": 100,
              "home_score": None, "away_score": None}
    rows = pl.DataFrame([active, dict(active, game_id="another"), dict(active, game_id="played", home_score=14)])
    prices = schedule_prices(rows, iter(["week11", "played"]))
    assert len(prices) == 4
    assert prices["week11", "moneyline", "LA", None] == {
        "american": 150, "decimal": 2.5, "source": "NFLverse schedule"}
    assert prices["week11", "moneyline", "SF", None]["decimal"] == 1.5
    assert prices["week11", "total", "Over", 43.5]["decimal"] == pytest.approx(1 + 100/120)
    assert prices["week11", "total", "Under", 43.5]["decimal"] == 2
    assert ("week11", "total", "Under", 44.5) not in prices


@pytest.mark.parametrize("invalid", [None, 0, -99, 1.91, float("nan"), float("inf"), True])
def test_missing_side_or_invalid_price_never_borrows_odds(invalid):
    rows = pl.DataFrame({"game_id": ["x"], "total_line": [42.5], "over_odds": [invalid],
                         "under_odds": [-112], "home_team": ["ARI"]})
    prices = schedule_prices(rows, ["x"])
    assert set(prices) == {("x", "total", "Under", 42.5)}
