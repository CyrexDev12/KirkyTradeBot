from datetime import datetime, timezone

from backend.trading.parlay import parlay_card
from bot.paper import publication_embed


def test_discord_board_shows_leg_prices_without_truncating_long_tickets():
    snapshot = datetime(2026, 11, 20, tzinfo=timezone.utc)
    legs = [{"market": "total", "matchup": f"AW{i} at HM{i}", "selection": "Over", "line": 45.5,
             "kickoff": "2026-11-22T18:00:00+00:00", "gradient_probability": .66, "mc_probability": .67,
             "odds": {"american": -200, "decimal": 1.5}} for i in range(5)]
    tickets = []
    for count in [2, 2, 3, 4, 5]:
        decimal = 1.5 ** count
        tickets.append({"profile": "Test mix", "legs": legs[:count], "reference_decimal_odds": decimal,
                        "reference_american_odds": round(100 * (decimal - 1)),
                        "market_implied_probability": 1 / decimal, "estimated_hit_probability": .66 ** count,
                        "recorded_at": snapshot.isoformat(), "overall_rating": 4.0, "risk_score": 7.0})
    card = parlay_card(2026, 11, tickets, snapshot, snapshot)
    embed = publication_embed(card, "parlay:2026:11")
    assert len(embed) <= 6000 and len(embed.fields) == 5
    assert all(value == rendered.value for (_, value), rendered in zip(card["fields"], embed.fields))
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert "2 legs" in embed.fields[0].name and "5 legs" in embed.fields[4].name
    first = embed.fields[0].value
    assert "-200 (1.50×)" in first and "66% / 67%" in first
    assert "2.25× return" in first and "(+125)" in first and "$10 → $22.50" in first
    assert "43.6%" in first and "break-even 44.4%" in first
    assert embed.fields[-1].value.count("Over 45.5") == 5
    assert "NFLverse reference odds" in embed.description and "promo boost" in embed.description

    tickets[0]["legs"] = [legs[0], {**legs[1], "odds": None}]
    tickets[0]["reference_decimal_odds"] = None
    text = parlay_card(2026, 11, tickets[:1], snapshot, snapshot)["fields"][0][1]
    assert "odds unavailable" in text and "1/2 legs" in text and "$10" not in text
