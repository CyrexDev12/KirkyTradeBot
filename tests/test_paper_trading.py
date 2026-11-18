"""Acceptance tests for the PAPER_ONLY trading subsystem (brief section 18)."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import re

import pytest

from backend.trading import candidates as cand
from backend.trading.book import BookSnapshot, parse_book, selected_view
from backend.trading.config import FROZEN_THESIS_PRICE_EXITS, PREGAME_ONLY, StrategyConfig
from backend.trading.contracts import MappingError, moneyline_spec, settlement_payout, total_spec
from backend.trading.fees import FeeProvider, FeeUnavailable, execution_fee
from backend.trading.mock_run import run as mock_run
from backend.trading.paper_engine import PaperEngine, TradePlan, deadlines
from backend.trading.path_model import CENSORED, AnalogueEstimator, bucket, label_path
from backend.trading.planner import WeeklyPlanner, plan_key
from backend.trading.jobs import WeeklyJobs, tuesday_fallback
from backend.trading.risk import Exposure, size
from backend.trading.scoring import Rating
from backend.trading.store import Store
from backend.trading.valuation import MC, TieEstimate, UncertaintyProfile, gb_moneyline, mc_moneyline
from backend.trading.venue import PaperOnlyViolation, check_request
from bot.paper import PaperPosts, PaperPublisher

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
KICK = T0 + timedelta(hours=6)
CFG = StrategyConfig()


def plan(trade_id="T-1", qty=40, entry="0.15", target="0.30", stop="0.11", side="YES", slug="m", game="g1",
         policy=FROZEN_THESIS_PRICE_EXITS, kickoff=KICK, **kw):
    exp, exit_dl = deadlines(kickoff, policy, CFG)
    base = dict(trade_id=trade_id, revision=1, strategy_id="s", config_version=CFG.version, season=2026, week=12,
                game_id=game, market_slug=slug, market_type="moneyline", selected_outcome="IND", selected_side=side,
                total_line=None, settlement_rule_version="moneyline-full-game-ot-tie-0.50-v1", hold_policy=policy,
                kickoff=kickoff, decision_at=T0, entry_active_after=T0, entry_expires_at=exp, exit_deadline=exit_dl,
                entry_limit=D(entry), target_limit=D(target), stop_trigger=D(stop), quantity=D(qty),
                tick_size=D("0.01"), quantity_step=D(1), min_quantity=D(1), estimated_entry_cost=D("6.48"),
                planned_loss=D("2.64"), full_funded_loss=D("6.48"))
    base.update(kw)
    return TradePlan(**base)


def snap(sec, bids, offers, slug="m", state="OPEN"):
    t = T0 + timedelta(seconds=sec)
    return BookSnapshot(slug, t, t, state, tuple((D(str(p)), D(str(q))) for p, q in bids),
                        tuple((D(str(p)), D(str(q))) for p, q in offers))


@pytest.fixture
def engine():
    e = PaperEngine(CFG)
    e.open_account("acct", T0)
    return e


def types(events):
    return [e["type"] for e in events]


# ------------------------------------------------------------------ fees/math
def test_mock_card_arithmetic_and_fee_profile():
    theta = D("0.095")
    assert execution_fee(theta, [(D("0.15"), D(40))]) == D("0.48")
    assert 40 * D("0.30") - execution_fee(theta, [(D("0.30"), D(40))]) - D("6.48") == D("4.72")
    assert D("6.48") - (40 * D("0.11") - execution_fee(theta, [(D("0.11"), D(40))])) == D("2.45")
    assert D("6.48") - (40 * D("0.105") - execution_fee(theta, [(D("0.105"), D(40))])) == D("2.64")
    fees = FeeProvider(profile_id=CFG.fee_profile_id)
    assert fees.profile_at(T0, "moneyline").theta("taker") == theta
    with pytest.raises(FeeUnavailable):
        fees.profile_at(datetime(2026, 11, 13, tzinfo=timezone.utc), "moneyline")  # before effective date
    with pytest.raises(FeeUnavailable):
        fees.profile_at(T0).theta("maker")  # never silently zero


# ---------------------------------------------------------- engine semantics
def test_working_entry_reserves_cash_but_is_not_a_position(engine):
    assert engine.submit(plan(), T0)["accepted"]
    a = engine.account(T0)
    assert D(a["reserved"]) == D("6.50") and D(a["cash"]) == D("100.00") and D(a["available_cash"]) == D("93.50")
    assert a["positions"] == [] and a["working_entries"][0]["remaining"] == "40"
    assert D(a["equity"]) == D("100.00")  # reservations are not counted twice


def test_touching_price_and_unfilled_expiry_release_reservation_without_loss(engine):
    engine.submit(plan(), T0)
    engine.on_snapshot(snap(5, [(0.15, 500)], [(0.16, 500)]))  # bid touches 15c; nothing offered at/below limit
    assert "ENTRY_FILL" not in types(engine.take_events())
    engine.on_clock(KICK - timedelta(minutes=10))
    a = engine.account(KICK)
    assert engine.state.orders["T-1"].status == "EXPIRED"
    assert D(a["cash"]) == D("100.00") and D(a["reserved"]) == 0 and D(a["realized_pnl"]) == 0


def test_latency_and_participation_cap_produce_proportional_partial_fills(engine):
    engine.submit(plan(), T0)
    engine.on_snapshot(snap(1, [(0.13, 100)], [(0.15, 100)]))  # inside the 2s activation latency
    assert engine.state.orders["T-1"].filled == 0
    engine.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 40)]))
    assert engine.state.orders["T-1"].filled == 20 and engine.state.positions["T-1"].qty_open == 20
    engine.on_snapshot(snap(20, [(0.30, 200)], [(0.31, 100)]))
    pos = engine.state.positions["T-1"]
    assert pos.qty_open == 0 and D(pos.exit_qty["TARGET"]) == 20  # exit coverage never exceeds inventory


def test_repeated_unchanged_book_cannot_supply_liquidity_twice(engine):
    engine.submit(plan(), T0)
    for sec in (5, 10, 15, 20):
        engine.on_snapshot(snap(sec, [(0.13, 100)], [(0.15, 40)]))
    assert engine.state.orders["T-1"].filled == 20
    engine.on_snapshot(snap(25, [(0.13, 100)], [(0.15, 80)]))  # fresh replenishment of 40
    assert engine.state.orders["T-1"].filled == 40


def test_partial_target_exit_cancels_entry_and_never_reopens(engine):
    engine.submit(plan(), T0)
    engine.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 40)]))
    engine.on_snapshot(snap(10, [(0.30, 10)], [(0.31, 100)]))  # sells 5
    o = engine.state.orders["T-1"]
    assert o.status == "CANCELED" and o.filled == 20 and o.reserved == 0
    engine.on_snapshot(snap(15, [(0.13, 100)], [(0.15, 500)]))  # cheap offers again
    assert engine.state.orders["T-1"].filled == 20
    assert engine.state.positions["T-1"].qty_bought == 20


def test_target_and_stop_share_inventory_and_stop_gap_books_actual_levels(engine):
    engine.submit(plan(), T0)
    engine.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 80)]))
    engine.on_snapshot(snap(10, [(0.30, 20)], [(0.31, 10)]))  # target sells 10 of 40
    engine.on_snapshot(snap(20, [(0.07, 100)], [(0.09, 10)]))  # gap far below the 11c trigger
    events = engine.take_events()
    stop_fill = [e for e in events if e["type"] == "EXIT_FILL" and e["reason"] == "STOP"][0]
    assert stop_fill["fills"] == [["0.07", "30"]]
    pos = engine.state.positions["T-1"]
    assert pos.qty_open == 0 and pos.qty_bought == 40
    assert sum(D(q) for q in pos.exit_qty.values()) == 40  # never sells more than owned


def test_stop_with_empty_bids_and_halted_market_keeps_position_open(engine):
    engine.submit(plan(), T0)
    engine.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 80)]))
    engine.on_snapshot(snap(10, [(0.10, 0.000001)], [(0.12, 10)]))  # trigger; virtually no executable size
    engine.on_snapshot(snap(15, [], [(0.12, 10)]))  # no bids at all
    pos = engine.state.positions["T-1"]
    assert pos.exit_mode == "STOP" and pos.qty_open == 40
    engine.on_clock(KICK + timedelta(hours=4))
    engine.on_snapshot(BookSnapshot("m", KICK + timedelta(hours=4, seconds=1), None, "HALTED",
                                    ((D("0.50"), D(100)),), ((D("0.52"), D(100)),)))
    assert engine.state.positions["T-1"].qty_open == 40  # deadlines never manufacture a fill


def test_feed_outage_never_fabricates_fills(engine):
    engine.submit(plan(), T0)
    engine.on_snapshot(snap(5, [(0.12, 100)], [(0.16, 100)]))
    engine.on_snapshot(snap(300, [(0.12, 100)], [(0.16, 100)]))  # 295s gap; prices in between unknown
    events = engine.take_events()
    assert "OUTAGE" in types(events) and "ENTRY_FILL" not in types(events)
    assert engine.state.outages and engine.state.orders["T-1"].filled == 0


def test_settlement_uses_verified_rules_and_cannot_be_booked_twice(engine):
    engine.submit(plan(qty=10, entry="0.60", target="0.75", stop="0.55"), T0)
    engine.on_snapshot(snap(5, [(0.58, 100)], [(0.60, 100)]))
    engine.on_snapshot(snap(10, [(0.62, 100)], [(0.64, 100)], state="RESOLVED"))
    assert engine.state.positions["T-1"].status == "SETTLEMENT_PENDING"
    for _ in range(2):
        engine.on_settlement("m", {"T-1": D("0.5")}, KICK + timedelta(hours=4), "tie")
    settled = [e for e in engine.take_events() if e["type"] == "SETTLED"]
    assert len(settled) == 1 and engine.state.positions["T-1"].qty_open == 0


def test_settlement_payout_rules():
    from backend.trading.contracts import ContractSpec
    common = dict(venue="v", market_slug="m", market_id=None, event_id=None, game_id="g", season=2026, week=12,
                  home="HOU", away="IND", kickoff=KICK, tick_size=D("0.01"), quantity_step=D(1), min_quantity=D(1))
    ml = ContractSpec(**common, market_type="moneyline", selected_outcome="IND", selected_side="NO", total_line=None,
                      tie_payout=D("0.5"), settlement_rule_version="x")
    assert settlement_payout(ml, 20, 20) == D("0.5") and settlement_payout(ml, 17, 20) == 1
    tot = ContractSpec(**common, market_type="total", selected_outcome="under", selected_side="NO",
                       total_line=D("41.5"), tie_payout=None, settlement_rule_version="x")
    assert settlement_payout(tot, 21, 20) == 1 and settlement_payout(tot, 21, 21) == 0


def test_kickoff_change_moves_pre_start_deadlines_only(engine):
    engine.submit(plan(), T0)
    later = KICK + timedelta(hours=3)
    engine.on_kickoff_change("g1", later, T0 + timedelta(minutes=1))
    p = engine.state.plans["T-1"]
    assert p.kickoff == later and p.entry_expires_at == later - timedelta(minutes=10) and p.revision == 2
    engine.on_game_started("g1", later + timedelta(minutes=1))
    engine.on_kickoff_change("g1", later + timedelta(hours=1), later + timedelta(minutes=2))
    assert engine.state.plans["T-1"].kickoff == later
    assert engine.state.orders["T-1"].status == "CANCELED"


def test_simultaneous_fills_cannot_exceed_cash(engine):
    accepted = 0
    for i in range(40):
        accepted += engine.submit(plan(trade_id=f"T-{i}", qty=100, game=f"g{i}"), T0)["accepted"]
    assert accepted == 6  # 6 x $16.03 reserved fits in $100; the 7th would not
    for i in range(40):
        engine.on_snapshot(snap(5 + i, [(0.13, 10_000)], [(0.15, 10_000)], slug="m"))
    assert engine.state.cash >= 0 and engine.state.available_cash >= 0


def test_sizing_applies_all_ceilings_assuming_working_entries_fill():
    asks = [(D("0.15"), D(10_000))]
    empty = Exposure(D(0), D(0), 0, {})
    r = size(rating=D("8.6"), equity=D(100), available_cash=D(100), exposure=empty, game_id="g", entry_limit=D("0.15"),
             stop_trigger=D("0.11"), theta=D("0.095"), selected_asks=asks, cfg=CFG)
    assert r.quantity > 0 and r.planned_loss <= D("3.00") and r.entry_cost <= D("15.00")
    crowded = Exposure(D("39.90"), D(0), 1, {})
    assert size(rating=D("8.6"), equity=D(100), available_cash=D(100), exposure=crowded, game_id="g",
                entry_limit=D("0.15"), stop_trigger=D("0.11"), theta=D("0.095"), selected_asks=asks,
                cfg=CFG).quantity == 0
    full = Exposure(D(0), D(0), 3, {})
    assert size(rating=D("8.6"), equity=D(100), available_cash=D(100), exposure=full, game_id="g",
                entry_limit=D("0.15"), stop_trigger=D("0.11"), theta=D("0.095"), selected_asks=asks,
                cfg=CFG).binding == "max concurrent positions"


def test_pregame_only_profile_exits_before_kickoff_and_frozen_profile_keeps_monitoring(engine):
    engine.submit(plan(policy=PREGAME_ONLY), T0)
    engine.submit(plan(trade_id="T-2", slug="m2", game="g2"), T0)
    for slug in ("m", "m2"):
        engine.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 80)], slug=slug))
    engine.on_clock(KICK - timedelta(minutes=5))
    assert engine.state.positions["T-1"].exit_mode == "TIME"
    assert engine.state.positions["T-2"].exit_mode is None  # frozen thesis: exits run until kickoff + 4h
    engine.on_clock(KICK + timedelta(hours=4))
    assert engine.state.positions["T-2"].exit_mode == "TIME"


def test_mc_moneyline_value_includes_tie_payout():
    v = mc_moneyline(MC_FC, "IND", "HOU", D("0.5"), PROFILE)
    assert v.fair_value == D("0.25") + D("0.5") * D("0.02") and v.lower == v.fair_value - D("0.03")


# ------------------------------------------------------------- persistence
def test_restart_preserves_account_positions_deadlines_and_notifications(tmp_path):
    store = Store(tmp_path / "p.sqlite3")
    e = store.load_engine(CFG)
    e.open_account("acct", T0)
    e.submit(plan(), T0)
    e.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 40)]))
    store.commit(e, T0, [{"publication_id": "x", "kind": "trade_alert", "channel": "updates", "payload": {}}],
                 write_memory=True)
    again = store.load_engine(CFG)
    assert again.account(T0 + timedelta(seconds=5)) == e.account(T0 + timedelta(seconds=5))
    assert again.state.plans["T-1"].exit_deadline == e.state.plans["T-1"].exit_deadline
    assert again.open_account("acct", T0) is None  # never re-initialized or replenished
    assert [p["publication_id"] for p in store.pending_publications()] == ["x"]
    # The same snapshot replayed after restart cannot fill again.
    again.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 40)]))
    assert again.state.orders["T-1"].filled == 20


def test_replay_is_deterministic_and_reconciles(tmp_path):
    a, b = mock_run(str(tmp_path / "a.db")), mock_run(str(tmp_path / "b.db"))
    strip = lambda evs: [{k: v for k, v in e.items() if k != "seq"} for e in evs]
    assert strip(a["events"]) == strip(b["events"]) and a["reconciled"]


def test_week_end_equity_includes_open_positions(engine):
    engine.submit(plan(), T0)
    engine.on_snapshot(snap(5, [(0.13, 100)], [(0.15, 80)]))
    engine.on_snapshot(snap(10, [(0.20, 100)], [(0.22, 100)]))
    a = engine.account(T0 + timedelta(seconds=10))
    assert D(a["equity"]) != D("100.00")
    assert D(a["equity"]) == D(a["cash"]) + D(a["open_liquidation_value"])
    assert D(a["open_liquidation_value"]) == D("8.00") - execution_fee(D("0.095"), [(D("0.20"), D(40))])


# ---------------------------------------------------------- mapping/models
EVENT = {"id": "1", "startTime": KICK.isoformat(), "teams": [{"abbreviation": "hou"}, {"abbreviation": "ind"}],
         "markets": [{"id": "mk", "slug": "nfl-ind-hou", "marketType": "moneyline",
                      "sportsMarketType": "football_team_full_game_winner", "active": True, "closed": False,
                      "tickSize": "0.01", "quantityIncrement": "1", "minimumQuantity": "1",
                      "description": "Overtime is included. If there is a tie, each side settles at $0.50.",
                      "marketSides": [{"long": True, "team": {"abbreviation": "ind", "ordering": "away"}},
                                      {"long": False, "team": {"abbreviation": "hou", "ordering": "home"}}]}]}
GAME = {"game_id": "2026_12_IND_HOU", "season": 2026, "week": 12, "game_type": "REG", "home": "HOU", "away": "IND",
        "kickoff": KICK.isoformat(), "home_score": None, "away_score": None, "kickoff_confirmed": True}


def ml_match(event=EVENT, game=GAME):
    from backend.markets import match_event
    return match_event(game, [event])


def test_contract_mapping_orientation_and_rejections():
    spec = moneyline_spec(GAME, ml_match(), "IND")
    assert spec.selected_side == "YES" and spec.tie_payout == D("0.5") and spec.increments_verified
    assert moneyline_spec(GAME, ml_match(), "HOU").selected_side == "NO"
    with pytest.raises(MappingError) as exc:
        moneyline_spec(GAME, ml_match(), "KC")
    assert exc.value.code == "CONTRACT_MAPPING_FAILED"
    swapped = deepcopy(ml_match())
    swapped["sides"]["IND"]["team"]["ordering"] = "home"
    with pytest.raises(MappingError):
        moneyline_spec(GAME, swapped, "IND")
    odd = deepcopy(ml_match())
    odd["market"]["description"] = "Overtime is included. Ties void at the discretion of $0.50 rules."
    with pytest.raises(MappingError) as exc:
        moneyline_spec(GAME, odd, "IND")
    assert exc.value.code == "UNSUPPORTED_SETTLEMENT"
    total = {"event": EVENT, "line": 41.5, "market": {"slug": "t", "description": "Teams combine for over 44.5 points. Overtime is included."},
             "sides": {"over": {"long": True}, "under": {"long": False}}}
    with pytest.raises(MappingError) as exc:
        total_spec(GAME, total, "over")  # description line disagrees with the market line
    assert exc.value.code == "UNSUPPORTED_SETTLEMENT"
    total["market"]["description"] = "Teams combine for over 41.5 points. Overtime is included."
    assert total_spec(GAME, total, "under").selected_side == "NO"
    total["line"] = 41.0
    with pytest.raises(MappingError):
        total_spec(GAME, total, "over")


def test_no_side_normalization_uses_complements():
    s = snap(0, [(0.70, 40), (0.65, 60)], [(0.72, 10)])
    view = selected_view(s, "NO")
    assert view.best_ask == D("0.30") and view.best_bid == D("0.28")
    book = parse_book("x", {"marketData": {"state": "MARKET_STATE_OPEN", "transactTime": T0.isoformat(),
                                           "bids": [{"px": {"value": "0.40"}, "qty": "5"}],
                                           "offers": [{"px": {"value": "0.42"}, "qty": "7"}], "bidDepth": 999}}, T0)
    assert book.bids == ((D("0.40"), D(5)),) and book.offers == ((D("0.42"), D(7)),)


PROFILE = UncertaintyProfile("X", "moneyline", D("0.03"), "test", 500, "test")
TIE = TieEstimate(D("0.004"), D("0.002"), D("0.008"), 3000, 12, "test")
MC_FC = {"status": "available", "model_version": "v", "game_id": GAME["game_id"], "as_of": T0.isoformat(),
         "probability_home_win": 0.73, "probability_away_win": 0.25, "probability_tie": 0.02}


def test_gb_conditional_probability_needs_payout_adapter():
    pred = {"home_probability": 0.78, "kind": "live_pregame", "model_version": "v", "generated_at": T0.isoformat()}
    v = gb_moneyline(pred, "IND", "HOU", D("0.5"), None, PROFILE)
    assert not v.available and v.code == "UNSUPPORTED_SETTLEMENT"
    v = gb_moneyline(pred, "IND", "HOU", D("0.5"), TIE, PROFILE)
    q = D("0.22")
    assert v.fair_value == (1 - TIE.rate) * q + D("0.5") * TIE.rate
    assert v.raw["conditional_win_probability"] == "0.22" and v.fair_value != q
    assert not gb_moneyline({**pred, "kind": "walk_forward_reconstruction"}, "IND", "HOU", D("0.5"), TIE, PROFILE).available


# -------------------------------------------------- strategy/planning layer
def examples(bucket_key, n_games=40, ret="0.25", weeks=8, label="TARGET"):
    rows = []
    for i in range(n_games):
        rows.append({"game_id": f"hist{i}", "season": 2025, "week": 1 + i % weeks, "bucket": bucket_key,
                     "label": label, "return": ret, "stress_floor_return": "-0.4",
                     "decision_at": "2025-10-01T00:00:00+00:00"})
    return rows


def grid_examples(ask=D("0.15"), ret="0.25"):
    out = []
    for k in CFG.grid_entry_ticks_below_ask:
        for t in CFG.grid_target_ticks_above_entry:
            for s in CFG.grid_stop_ticks_below_entry:
                e = ask - D("0.01") * k
                out += examples(bucket("moneyline", CFG.hold_policy, e, e + D("0.01") * t, e - D("0.01") * s), ret=ret)
    return out


class FakeInputs:
    def __init__(self, book_snap, mc=MC_FC, ready=True):
        self.snap, self.mc, self.ready = book_snap, mc, ready
        self.games_rows = [GAME]

    def all_games(self):
        return self.games_rows

    def games(self, season, week):
        return [g for g in self.games_rows if (g["season"], g["week"]) == (season, week)]

    def gb_prediction(self, game):
        return {"home_probability": 0.78, "kind": "live_pregame", "model_version": "v", "generated_at": T0.isoformat()}

    def gb_total_prediction(self, game):
        return None

    def gb_total_over(self, game, line):
        return None

    def mc_forecast(self, game):
        return self.mc

    def mc_total(self, fc, line):
        return None

    def tie_estimate(self):
        return TIE

    def uncertainty(self, provider, market_type):
        return PROFILE

    async def markets(self, games):
        return {GAME["game_id"]: {"moneyline": ml_match(), "totals": []}}

    async def book(self, slug):
        return self.snap

    def ready_for(self, season, week, now):
        return (True, None) if self.ready else (False, "GB predictions not refreshed since the previous slate's results")


def book_now(now):
    return BookSnapshot("nfl-ind-hou", now, now, "OPEN", ((D("0.13"), D(400)),), ((D("0.15"), D(400)),))


def planner_with(tmp_path, examples_rows=(), validated=True, inputs=None, now=T0):
    store = Store(tmp_path / "plan.sqlite3")
    if examples_rows:
        store.add_path_examples(list(examples_rows), now)
    if validated:
        buckets = {x["bucket"]: {"passed": True} for x in examples_rows}
        store.save_validation(CFG.version, bool(buckets), {"buckets": buckets, "reasons": []}, now)
    return store, WeeklyPlanner(store, CFG, inputs or FakeInputs(book_now(now)))


def test_no_history_week_creates_zero_orders_and_explains(tmp_path):
    store, planner = planner_with(tmp_path)
    plan_row = asyncio.run(planner.run(2026, 12, T0))
    s = plan_row["summary"]
    assert s["qualifying_trades"] == 0
    assert s["headline"] == "No rated trades this week — insufficient market-path history to estimate scalp outcomes."
    assert "INSUFFICIENT_EXECUTION_HISTORY" in s["primary_reasons"]
    assert store.load_engine(CFG).state.orders == {}
    pubs = store.pending_publications()
    assert pubs and "insufficient market-path history" in pubs[0]["payload"]["description"]


def test_missing_mc_never_falls_back_to_gb(tmp_path):
    inputs = FakeInputs(book_now(T0), mc={"status": "unavailable", "unavailable_reason": "Insufficient sample"})
    store, planner = planner_with(tmp_path, grid_examples(), inputs=inputs)
    plan_row = asyncio.run(planner.run(2026, 12, T0))
    assert plan_row["summary"]["qualifying_trades"] == 0
    codes = {c for d in plan_row["payload"]["decisions"] for c in d["rejection_codes"]}
    assert "MC_UNAVAILABLE" in codes
    for d in plan_row["payload"]["decisions"]:
        assert d["valuations"]["MC"]["fair_value"] is None


def test_supported_trade_is_planned_rated_sized_and_activated_once(tmp_path):
    store, planner = planner_with(tmp_path, grid_examples())
    plan_row = asyncio.run(planner.run(2026, 12, T0))
    assert plan_row["summary"]["qualifying_trades"] == 1
    trade = plan_row["payload"]["trades"][0]
    assert trade["selected_outcome"] == "IND" and D(trade["meta"]["rating"]["value"]) >= D("6.5")
    assert D(trade["planned_loss"]) <= D("0.03") * 100  # rating-tier ceiling of equity
    hou = [d for d in plan_row["payload"]["decisions"] if d["selected_outcome"] == "HOU"][0]
    assert hou["primary_code"] == "NO_MODEL_CONSENSUS"
    key = plan_key(CFG, 2026, 12)
    later = T0 + timedelta(minutes=1)
    planner.inputs.snap = book_now(later)
    first = asyncio.run(planner.activate(key, later))
    assert first and first[0]["status"] == "WORKING"
    # Duplicate triggers: same plan returned, no second set of orders.
    assert asyncio.run(planner.run(2026, 12, later))["plan_key"] == key
    assert asyncio.run(planner.activate(key, later)) == []
    assert len(store.load_engine(CFG).state.orders) == 1


def test_negative_expectancy_rejected_even_if_rating_would_be_high(tmp_path):
    store, planner = planner_with(tmp_path, grid_examples(ret="-0.05"))
    plan_row = asyncio.run(planner.run(2026, 12, T0))
    ind = [d for d in plan_row["payload"]["decisions"] if d["selected_outcome"] == "IND"][0]
    assert not ind["accepted"] and ind["primary_code"] == "NEGATIVE_NET_EXPECTANCY" and ind["rating"] is None


def test_rating_thresholds_and_missing_evidence(tmp_path, monkeypatch):
    for value, accepted in ((D("6.4"), False), (D("9.0"), True)):
        store, planner = planner_with(tmp_path / str(value), grid_examples())
        monkeypatch.setattr(cand, "_rating", lambda *a, value=value, **k: Rating(value, {"economics": D(1)}))
        row = asyncio.run(planner.run(2026, 12, T0))
        ind = [d for d in row["payload"]["decisions"] if d["selected_outcome"] == "IND"][0]
        assert ind["accepted"] is accepted
        if not accepted:
            assert "RATING_BELOW_MINIMUM" in ind["rejection_codes"]
    from backend.trading.scoring import combine
    r = combine({"economics": D(1), "execution": None, "evidence": D(1), "model_support": D(1), "portfolio_fit": D(1)})
    assert r.value is None and r.missing == ("execution",)


def test_final_size_recheck_catches_fee_rounding_at_the_actual_quantity():
    # Reference all-in cost at 50c is 0.5238/contract (100-lot, cent-rounded fee); at exactly 2 contracts it is 0.525.
    gb = gb_moneyline({"home_probability": 0.5, "kind": "live_pregame"}, "IND", "HOU", D("0.5"),
                      TieEstimate(D(0), D(0), D(0), 1, 0, "t"), UncertaintyProfile("GB", "moneyline", D(0), "t", 1, "t"))
    lower = D("0.5438")
    gb = gb.__class__(**{**gb.__dict__, "lower": lower, "fair_value": lower})
    mc = gb.__class__(**{**gb.__dict__, "provider": MC})
    s = BookSnapshot("nfl-ind-hou", T0, T0, "OPEN", ((D("0.48"), D(100)),), ((D("0.50"), D(4)),))
    spec = moneyline_spec(GAME, ml_match(), "IND")
    inp = cand.CandidateInput(GAME, "moneyline", "IND", spec, None, gb, mc, s, T0)
    cfg = CFG.with_overrides(grid_entry_ticks_below_ask=(0,), rated_trades_require_validation=False)
    est = AnalogueEstimator(grid_examples(D("0.50")), cfg)
    d = cand.evaluate(inp, cfg, D("0.095"), est, None)
    assert any(c.partial_rating for c in d.combos)  # passes at the reference size
    d = cand.finalize(d, inp, cfg, D("0.095"), equity=D(100), available_cash=D(100), exposure=Exposure(D(0), D(0), 0, {}),
                      suspended=None, trade_id="X", plan_key="k")
    assert not d.accepted and any("final size" in n for n in d.notes)


def test_missing_paths_are_censored_not_zero_and_future_examples_excluded():
    p = plan(policy=FROZEN_THESIS_PRICE_EXITS)
    snaps = [snap(5, [(0.13, 100)], [(0.15, 80)]), snap(400, [(0.30, 100)], [(0.31, 100)])]
    label = label_path(p, snaps, CFG, settlement_payout=D(1))
    assert label["label"] == CENSORED and label["return"] is None
    key = label["bucket"]
    rows = examples(key, n_games=40) + [{**label, "decision_at": T0.isoformat()} for _ in range(10)]
    est = AnalogueEstimator(rows, CFG).estimate(key)
    assert est.censored_fraction == D(10) / D(50) and not est.supported  # withheld, not renormalized
    future = examples(key, n_games=40)
    for r in future:
        r["decision_at"] = (T0 + timedelta(days=1)).isoformat()
    assert AnalogueEstimator(future, CFG).estimate(key, before=T0).examples == 0


def test_replay_labels_use_the_same_fill_policy():
    p = plan()
    snaps = [snap(5, [(0.13, 100)], [(0.15, 80)]), snap(30, [(0.30, 200)], [(0.31, 10)])]
    label = label_path(p, snaps, CFG)
    assert label["label"] == "TARGET" and D(label["return"]) > 0
    touched_only = [snap(5, [(0.13, 100)], [(0.16, 80)]), snap(30, [(0.15, 100)], [(0.16, 10)]),
                    snap(60, [(0.15, 100)], [(0.16, 10)])]
    assert label_path(p, touched_only, CFG)["label"] == CENSORED  # unresolved: never a fill on a touch


# -------------------------------------------------------------- scheduling
def test_late_nfl_data_delays_plan_but_not_account_report(tmp_path):
    prev = {**GAME, "game_id": "prev", "week": 11, "kickoff": (T0 - timedelta(days=6)).isoformat(),
            "home_score": 20, "away_score": 17}
    inputs = FakeInputs(book_now(T0), ready=False)
    inputs.games_rows = [prev, GAME]
    store = Store(tmp_path / "j.sqlite3")
    planner = WeeklyPlanner(store, CFG, inputs)
    jobs = WeeklyJobs(store, CFG, planner, inputs)
    asyncio.run(jobs.tick(T0))
    actions = asyncio.run(jobs.tick(T0 + timedelta(minutes=31)))
    assert actions == ["report:nfl-dual-model-price-scalp:2026:11"]
    assert store.weekly_plan(plan_key(CFG, 2026, 12)) is None
    assert store.job(f"plan:{plan_key(CFG, 2026, 12)}")["status"] == "waiting_data"
    kinds = [p["kind"] for p in store.pending_publications()]
    assert "weekly_report" in kinds
    snaps = store.account_snapshots("weekly")
    assert snaps and D(snaps[0]["equity"]) == D("100.00")


def test_tuesday_fallback_is_phoenix_time():
    monday_night = {"kickoff": "2026-11-24T01:15:00+00:00"}  # Monday 6:15 PM Phoenix
    assert tuesday_fallback([monday_night], CFG).isoformat() == "2026-11-24T15:00:00+00:00"


# ------------------------------------------------------------------ Discord
class FakeTransport:
    def __init__(self):
        self.sent, self.edits, self.fail_after_send = {}, [], False

    async def find_existing(self, channel_id, pid, started_at):
        return self.sent.get(pid)

    async def send(self, channel_id, embed, nonce):
        pid = embed.footer.text.split("pub ")[-1]
        assert pid not in self.sent, "blind resend"
        self.sent[pid] = 555
        if self.fail_after_send:
            raise TimeoutError()
        return 555

    async def edit(self, channel_id, message_id, embed):
        self.edits.append((message_id, embed.title))


class FakeAPI:
    def __init__(self, items):
        self.items, self.acks = items, []

    async def paper_outbox(self):
        return {"publications": [i for i in self.items if not any(a[0] == i["publication_id"] and a[1] == i["revision"]
                                                                   and a[2] for a in self.acks)]}

    async def paper_ack(self, pid, revision, message_id, error):
        self.acks.append((pid, revision, message_id, error))


def test_crash_during_publish_is_reconciled_and_cards_edit_in_place(tmp_path):
    posts = PaperPosts(tmp_path / "bot.sqlite3")
    posts.set_channel(1, "picks", 10)
    item = {"publication_id": "trade:T-1", "kind": "trade_card", "channel": "picks", "revision": 1,
            "payload": {"title": "PAPER TRADE | T-1 | WORKING ENTRY", "fields": [], "footer": "PAPER TRADE"}}
    api, transport = FakeAPI([item]), FakeTransport()
    publisher = PaperPublisher(api, posts, transport)
    transport.fail_after_send = True
    asyncio.run(publisher.tick())
    assert api.acks[-1][3] == "TimeoutError"
    transport.fail_after_send = False
    asyncio.run(publisher.tick())  # reconciles via history; would assert on a blind resend
    assert api.acks[-1][2] == "555" and len(transport.sent) == 1
    api.items = [{**item, "revision": 2, "payload": {**item["payload"], "title": "PAPER TRADE | T-1 | CLOSED"}}]
    asyncio.run(publisher.tick())
    assert transport.edits == [(555, "PAPER TRADE | T-1 | CLOSED")] and len(transport.sent) == 1


# -------------------------------------------------------------- paper only
def test_paper_only_cannot_reach_order_submission():
    for method, path in (("POST", "/v1/orders"), ("GET", "/v1/orders"), ("DELETE", "/v1/orders/1"),
                         ("POST", "/v1/markets/x/book"), ("GET", "/v1/portfolio/positions")):
        with pytest.raises(PaperOnlyViolation):
            check_request(method, path)
    check_request("GET", "/v1/markets/nfl-ind-hou/book")
    with pytest.raises(ValueError):
        StrategyConfig(real_order_execution=True)
    with pytest.raises(ValueError):
        StrategyConfig(mode="LIVE")
    root = Path(__file__).resolve().parents[1] / "backend"
    for file in root.rglob("*.py"):
        text = file.read_text()
        # The backend never issues an HTTP POST and never names an order route (route decorators are allowed).
        assert not re.search(r"/v1/orders|create[-_]?order|(client|httpx|session|venue)\.(post|put|delete)\(",
                             text, re.I), file


def test_paper_api_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app
    from backend.trading import api
    store = Store(tmp_path / "api.sqlite3")
    e = store.load_engine(CFG)
    e.open_account("acct", T0)
    store.commit(e, T0)
    monkeypatch.setattr(api, "_store", store)
    client = TestClient(app.app)
    assert client.get("/api/paper/account").json()["equity"] == "100.00"
    assert client.get("/api/paper/picks").json()["plan"] is None
    assert client.post("/api/paper/pause", json={"actor": "x"}).status_code == 403
    monkeypatch.setenv("NFL_PAPER_OPERATOR_TOKEN", "secret")
    r = client.post("/api/paper/pause", json={"actor": "x", "reason": "test"},
                    headers={"X-Paper-Operator-Token": "secret"})
    assert r.status_code == 200 and client.get("/api/paper/account").json()["paused"]
    assert client.get("/api/paper/why-no-trades").json()["readiness"]["path_examples"] == 0
