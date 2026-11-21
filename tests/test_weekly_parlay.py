import asyncio
from datetime import datetime, timedelta, timezone
import math

from backend.trading import parlay
from backend.trading.config import StrategyConfig
from backend.trading.store import Store
from backend.trading.worker import Worker


FRIDAY = datetime(2026, 11, 20, 20, tzinfo=timezone.utc)


def game(gid, home, away, hours):
    return {"game_id": gid, "season": 2026, "week": 11, "game_type": "REG", "kickoff_confirmed": True,
            "home": home, "away": away, "kickoff": (FRIDAY + timedelta(hours=hours)).isoformat(),
            "home_score": None, "away_score": None}


ROWS = [game("thursday", "IND", "HOU", -18), game("sun1", "GB", "CHI", 46), game("sun2", "LA", "SEA", 48)]


def forecasts(games):
    gb = {"kind": "live_pregame", "generated_at": (FRIDAY - timedelta(hours=2)).isoformat()}
    ml = {g["game_id"]: dict(gb, home_probability=(0.74 if g["game_id"] == "sun1" else 0.50)) for g in games}
    totals = {g["game_id"]: dict(gb, predicted_total=39) for g in games}
    mc = {g["game_id"]: {"status": "available", "forecast_origin": "archived", "kickoff": g["kickoff"],
                         "probability_home_win": 0.72, "probability_away_win": 0.25, "probability_tie": 0.03,
                         "simulation_count": 100, "distributions": {"total": {"histogram": [[38, 80], [48, 20]]}}}
          for g in games}
    return ml, totals, mc, {g["game_id"]: 42.5 for g in games}


def priced_forecasts(games):
    prices = {}
    for i, g in enumerate(games):
        prices[g["game_id"], "total", "Under", 42.5] = {
            "american": 100 + i * 10, "decimal": (2 + i / 10), "source": "NFLverse schedule"}
        prices[g["game_id"], "moneyline", g["home"], None] = {
            "american": -160, "decimal": 1.625, "source": "NFLverse schedule"}
    return (*forecasts(games), prices)


def test_selects_two_distinct_unstarted_games_with_listed_total(monkeypatch):
    ml, totals, mc, lines = forecasts(ROWS)
    lines["sun1"] = 42  # integer lines can push
    monkeypatch.setattr(parlay, "over_probability", lambda pred, line: 0.22)
    legs = parlay.select_legs(ROWS, ml, totals, mc, lines, FRIDAY)
    assert [(l["game_id"], l["selection"], l["line"]) for l in legs] == [("sun2", "Under", 42.5), ("sun1", "GB", None)]
    assert legs[0]["mc_probability"] == 0.8
    mc["sun2"]["status"] = "unavailable"
    assert not parlay.select_legs(ROWS, ml, totals, mc, lines, FRIDAY)
    mc["sun2"]["status"] = "available"
    ml["sun1"]["kind"] = "walk_forward_reconstruction"
    assert not parlay.select_legs(ROWS, ml, totals, mc, lines, FRIDAY)


def test_worker_weekly_job_catches_up_after_thursday_and_retries_until_pair(tmp_path, monkeypatch):
    store = Store(tmp_path / "paper.sqlite3")
    class Inputs:
        def all_games(self):
            return ROWS

    worker = Worker(store, StrategyConfig(), object(), Inputs())
    publisher = worker.jobs.parlay
    publisher.clock = lambda: FRIDAY
    publisher.load = lambda games: ({}, {}, {}, {})
    # Isolate parlay effects from the ordinary worker's initial account creation.
    worker.jobs.ensure_account(FRIDAY)
    events = store.ledger_events()
    asyncio.run(worker.jobs.tick(FRIDAY))
    assert store.latest_parlay()["status"] == "no_pair"
    publisher.load = forecasts
    later = FRIDAY + timedelta(minutes=11)
    publisher.clock = lambda: later
    monkeypatch.setattr(parlay, "over_probability", lambda pred, line: 0.22)
    asyncio.run(worker.jobs.tick(later))
    item = store.publication("parlay:2026:11")
    assert item["revision"] == 2 and item["channel"] == "parlay"
    assert len({l["game_id"] for l in item["payload"]["legs"]}) == 2
    assert "thursday" not in {l["game_id"] for l in item["payload"]["legs"]}
    assert store.ledger_events() == events
    assert store.load_engine(StrategyConfig()).state.orders == {}
    restarted = parlay.ParlayPublisher(store, load=forecasts, clock=lambda: later + timedelta(minutes=15))
    restarted.tick(ROWS, later + timedelta(minutes=15))
    assert store.publication("parlay:2026:11") == item


def test_full_board_contains_five_distinct_rated_tickets_and_is_frozen(tmp_path, monkeypatch):
    store = Store(tmp_path / "paper.sqlite3")
    games = ROWS + [game("sun3", "ATL", "TB", 52), game("sun4", "NYG", "PHI", 53),
                    game("sun5", "KC", "LV", 54), game("sun6", "SF", "SEA", 58)]
    monkeypatch.setattr(parlay, "over_probability", lambda pred, line: 0.22)
    worker = parlay.ParlayPublisher(store, load=priced_forecasts, clock=lambda: FRIDAY)
    worker.tick(games, FRIDAY)
    card = store.latest_parlay()
    tickets = card["parlays"]
    assert len(tickets) == len({parlay.ticket_key(t) for t in tickets}) == len(card["fields"]) == 5
    assert card["legs"] == tickets[0]["legs"]
    assert [len(t["legs"]) for t in tickets] == [2, 2, 3, 4, 5]
    assert not ({leg["game_id"] for leg in tickets[0]["legs"]} &
                {leg["game_id"] for leg in tickets[1]["legs"]})
    for t in tickets:
        legs = t["legs"]
        assert len({leg["game_id"] for leg in legs}) == len(legs)
        support = [min(l["gradient_probability"], l["mc_probability"]) for l in legs]
        assert t["estimated_hit_probability"] == math.prod(support)
        assert t["risk_score"] == round(1 + 9 * (1 - math.prod(support)), 1)
        assert t["overall_rating"] == round(1 + 18 * (min(support) - .5), 1)
        assert t["reference_decimal_odds"] == math.prod(l["odds"]["decimal"] for l in legs)
        assert t["market_implied_probability"] == 1 / t["reference_decimal_odds"]
        assert t["mc_hit_probability"] == math.prod(l["mc_probability"] for l in legs)
    assert tickets[-1]["risk_score"] > tickets[0]["risk_score"]
    assert tickets[-1]["reference_decimal_odds"] > tickets[0]["reference_decimal_odds"]
    worker.load = lambda _: (_ for _ in ()).throw(AssertionError("full board refetched"))
    worker.tick(games, FRIDAY + timedelta(hours=1))
    assert store.latest_parlay() == card


def test_upgrades_delivered_legacy_ticket_and_fills_without_changing_published_picks(tmp_path, monkeypatch):
    store = Store(tmp_path / "paper.sqlite3")
    monkeypatch.setattr(parlay, "over_probability", lambda pred, line: 0.22)
    legacy = parlay.select_legs(ROWS, *forecasts(ROWS), FRIDAY)
    store.enqueue([{"publication_id": "parlay:2026:11", "kind": "weekly_parlay", "channel": "parlay",
                    "revision": 2, "payload": {"status": "ready", "legs": legacy}}], FRIDAY)
    store.ack_publication("parlay:2026:11", 2, "1234", None, FRIDAY)
    worker = parlay.ParlayPublisher(store, load=forecasts, clock=lambda: FRIDAY)
    worker.tick(ROWS, FRIDAY)
    partial = store.publication("parlay:2026:11")
    assert partial["revision"] == 3
    assert partial["status"] == "pending" and partial["message_id"] == "1234"
    tickets = partial["payload"]["parlays"]
    assert 1 <= len(tickets) < 5 and tickets[0]["legs"] == legacy
    later = FRIDAY + timedelta(minutes=11)
    worker.clock = lambda: later
    worker.tick(ROWS + [game("extra", "SEA", "DET", 50), game("next", "LV", "KC", 52),
                        game("late", "SF", "PHI", 53)], later)
    final = store.publication("parlay:2026:11")
    assert final["revision"] == 4 and len(final["payload"]["parlays"]) == 5
    assert final["payload"]["parlays"][:len(tickets)] == tickets


def test_preseason_and_offseason_time_do_not_publish(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    publisher = parlay.ParlayPublisher(store, load=lambda _: (_ for _ in ()).throw(AssertionError("loaded")))
    publisher.tick(ROWS, FRIDAY - timedelta(days=15))
    publisher.tick([dict(g, game_type="POST") for g in ROWS], FRIDAY)
    assert store.latest_parlay() is None


def test_migrates_only_unstarted_long_slots_and_retains_replaced_picks(tmp_path, monkeypatch):
    games = ROWS + [game(f"late{n}", "NYG", "PHI", 50 + n) for n in range(4)]
    monkeypatch.setattr(parlay, "over_probability", lambda pred, line: 0.22)
    legs = {leg["game_id"]: leg for leg in parlay.eligible_legs(games, *forecasts(games), FRIDAY)}
    first, second = legs["sun1"], legs["sun2"]
    past = dict(first, game_id="thursday", kickoff=ROWS[0]["kickoff"])
    old = [{"legs": [a, b]} for a, b in [
        (first, second), (legs["late0"], legs["late1"]), (past, first),
        (legs["late0"], second), (first, legs["late1"])]]
    store = Store(tmp_path / "paper.sqlite3")
    store.enqueue([{"publication_id": "parlay:2026:11", "kind": "weekly_parlay", "channel": "parlay",
                    "revision": 6, "payload": {"as_of": (FRIDAY - timedelta(days=1)).isoformat(),
                                               "parlays": old}}], FRIDAY)
    worker = parlay.ParlayPublisher(store, load=priced_forecasts, clock=lambda: FRIDAY)
    worker.tick(games, FRIDAY)
    card = store.latest_parlay()
    assert [len(t["legs"]) for t in card["parlays"]] == [2, 2, 2, 4, 5]
    assert card["parlays"][2]["legs"] == old[2]["legs"]  # Already kicked off.
    assert [t["legs"] for t in card["superseded_parlays"]] == [t["legs"] for t in old[3:]]
    assert "revised" in card["description"]
    assert store.publication("parlay:2026:11")["revision"] == 7
    worker.tick(games, FRIDAY + timedelta(minutes=15))
    assert store.latest_parlay() == card


def test_fills_missing_quote_from_complete_matching_snapshot_without_changing_legs(tmp_path, monkeypatch):
    monkeypatch.setattr(parlay, "over_probability", lambda pred, line: 0.22)
    store = Store(tmp_path / "paper.sqlite3")
    models = priced_forecasts(ROWS)
    quotes = models[-1]
    partial = {k: q for k, q in quotes.items() if k[0] == "sun1"}
    worker = parlay.ParlayPublisher(store, load=lambda _: (*models[:4], partial), clock=lambda: FRIDAY)
    worker.tick(ROWS, FRIDAY)
    old = store.latest_parlay()["parlays"][0]
    assert old["reference_decimal_odds"] is None
    assert sum(bool(leg.get("odds")) for leg in old["legs"]) == 1
    later = FRIDAY + timedelta(minutes=15)
    wrong_line = {(gid, market, selection, 44.5): q for (gid, market, selection, _), q in quotes.items()}
    worker.load = lambda _: (*models[:4], wrong_line)
    worker.clock = lambda: later
    worker.tick(ROWS, later)
    assert store.latest_parlay()["parlays"][0] == old
    latest = later + timedelta(minutes=15)
    updated = {k: {"american": 200, "decimal": 3.0, "source": "NFLverse schedule"} for k in quotes}
    worker.load = lambda _: (*models[:4], updated)
    worker.clock = lambda: latest
    worker.tick(ROWS, latest)
    new = store.latest_parlay()["parlays"][0]
    assert new["recorded_at"] == latest.isoformat()
    assert parlay.ticket_key(new) == parlay.ticket_key(old)
    assert new["reference_decimal_odds"] == 9
    assert new["estimated_hit_probability"] == old["estimated_hit_probability"]


def test_long_shot_prefers_higher_available_return_among_eligible_distinct_games():
    def leg(gid, market, price):
        return {"game_id": gid, "market": market, "selection": "home" if market == "moneyline" else "Under",
                "odds": {"decimal": price}, "gradient_probability": .65, "mc_probability": .64}
    candidates = [leg(i, "moneyline", 1.5) for i in range(6)]
    pricey = [leg(i, "total", 2.0 + .1 * i) for i in range(5)]
    previous = [{"legs": pricey[:2]}, {"legs": pricey[2:4]}]
    pick = parlay.choose_ticket(5, candidates + pricey, previous, FRIDAY.isoformat())
    assert {l["game_id"] for l in pick["legs"]} == set(range(5))
    assert all(l["market"] == "total" for l in pick["legs"])
    assert math.isclose(pick["reference_decimal_odds"], math.prod(l["odds"]["decimal"] for l in pricey))


def test_channel_filter_applies_before_pending_limit(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    for i in range(25):
        store.enqueue([{"publication_id": f"picks:{i}", "kind": "weekly_plan", "channel": "picks", "payload": {}}], FRIDAY)
    for year in (2000, 2090):
        store.enqueue([{"publication_id": f"parlay:{year}:11", "kind": "weekly_parlay", "channel": "parlay",
                        "payload": {"expires_at": f"{year}-12-01T00:00:00+00:00"}}], FRIDAY)
    assert [p["publication_id"] for p in store.pending_publications(1, ["parlay"])] == ["parlay:2090:11"]


def test_public_parlay_and_scoped_delivery_endpoints(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.trading import api

    store = Store(tmp_path / "paper.sqlite3")
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "operator_token", lambda: "test-operator")
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    assert client.get("/api/paper/parlay").json()["card"] is None
    card = {"title": "Weekly parlay", "status": "ready", "expires_at": "2090-01-01T00:00:00+00:00"}
    store.enqueue([{"publication_id": "parlay:2026:11", "kind": "weekly_parlay", "channel": "parlay", "payload": card},
                   {"publication_id": "weekly:test", "kind": "weekly_plan", "channel": "picks", "payload": {}}], FRIDAY)
    assert client.get("/api/paper/parlay").json()["card"] == card
    path = "/api/paper/outbox/pending?channels=parlay&channels=reports"
    assert client.get(path).status_code == 403
    result = client.get(path, headers={"X-Paper-Operator-Token": "test-operator"}).json()
    assert [p["publication_id"] for p in result["publications"]] == ["parlay:2026:11"]
