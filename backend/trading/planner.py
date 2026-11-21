"""Weekly planning: fixed as-of snapshot, screening, portfolio selection, revalidation.

One authoritative plan per (strategy, season, week). Running the planner again
returns the stored plan; it never creates a second set of paper orders.
Accepted trades are activated only after revalidation against a fresh book.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import logging

from .book import is_fresh, selected_view
from .candidates import CandidateInput, evaluate, finalize
from .common import ZERO, D, dumps, iso
from .config import StrategyConfig
from .contracts import MappingError, moneyline_spec, total_spec
from .path_model import AnalogueEstimator
from .reports import no_trade_text, plan_publication, trade_card_publication
from .risk import Exposure, exposure as current_exposure, suspension
from .store import Store
from .valuation import (GB, MC, gb_moneyline, gb_total, mc_moneyline, mc_total, unavailable)

logger = logging.getLogger(__name__)


def plan_key(cfg: StrategyConfig, season: int, week: int) -> str:
    return f"{cfg.strategy_id}:{season}:{week:02d}"


class WeeklyPlanner:
    def __init__(self, store: Store, cfg: StrategyConfig, inputs, fees=None, progress=None):
        self.store, self.cfg, self.inputs, self.fees = store, cfg, inputs, fees
        self.progress = progress
        self._status = {}

    def _report(self, stage, **values):
        self._status.update(values, stage=stage, updated_at=iso(datetime.now(timezone.utc)))
        if self.progress:
            self.progress(dict(self._status))

    def _theta(self, engine, now, market_type):
        return engine.theta(now, market_type)

    async def candidates(self, season: int, week: int, now: datetime) -> list[CandidateInput]:
        games = [g for g in self.inputs.games(season, week) if datetime.fromisoformat(g["kickoff"]) > now]
        self._status = {"season": season, "week": week, "games_total": len(games), "games_completed": 0,
                        "books_requested": 0, "books_received": 0}
        self._report("discovering_markets")
        logger.info("Planning %s week %s: discovering markets for %s games", season, week, len(games))
        markets = await self.inputs.markets(games)
        tie = self.inputs.tie_estimate()
        out = []
        # Both outcomes use the same canonical book. Scope reuse to this planning
        # pass only; activation must still request a fresh book independently.
        books = {}

        async def book(spec):
            if spec is None:
                return None
            slug = spec.market_slug
            if slug not in books:
                self._report("fetching_books", market_slug=slug, books_requested=len(books) + 1)
                books[slug] = await self.inputs.book(slug)
                self._report("fetching_books", books_received=sum(s is not None for s in books.values()))
            return books[slug]

        for game in games:
            self._report("preparing_game", game_id=game["game_id"], market_slug=None)
            gm = markets.get(game["game_id"]) or {}
            gb_pred, mc_fc = self.inputs.gb_prediction(game), self.inputs.mc_forecast(game)
            ml = gm.get("moneyline")
            for team in (game["home"], game["away"]):
                spec, err = None, None
                try:
                    spec = moneyline_spec(game, ml, team)
                except MappingError as exc:
                    err = exc
                tie_payout = spec.tie_payout if spec else None
                gb = gb_moneyline(gb_pred, team, game["home"], tie_payout, tie,
                                  self.inputs.uncertainty(GB, "moneyline")) if spec else unavailable(GB, "no contract")
                mc = mc_moneyline(mc_fc, team, game["home"], tie_payout,
                                  self.inputs.uncertainty(MC, "moneyline")) if spec else unavailable(MC, "no contract")
                snap = await book(spec)
                if spec:
                    self.store.record_contract(spec, now)  # enables later replay labeling of recorded books
                out.append(CandidateInput(game, "moneyline", team, spec, err, gb, mc, snap, now))
            for match in gm.get("totals") or []:
                for outcome in ("over", "under"):
                    spec, err = None, None
                    try:
                        spec = total_spec(game, match, outcome)
                    except MappingError as exc:
                        err = exc
                    line = float(match["line"])
                    gbt = self.inputs.gb_total_prediction(game)
                    gb = gb_total(self.inputs.gb_total_over(game, line), outcome, gbt,
                                  self.inputs.uncertainty(GB, "total"))
                    mc = mc_total(self.inputs.mc_total(mc_fc, line), outcome, mc_fc, self.inputs.uncertainty(MC, "total"))
                    snap = await book(spec)
                    if spec:
                        self.store.record_contract(spec, now)
                    out.append(CandidateInput(game, "total", outcome, spec, err, gb, mc, snap, now))
            if not ml and not gm.get("totals"):
                err = MappingError("NO_LISTED_MARKET", "No verified moneyline or full-game total contract")
                out.append(CandidateInput(game, "moneyline", "-", None, err, unavailable(GB, "no market"),
                                          unavailable(MC, "no market"), None, now))
            self._report("collecting", games_completed=self._status["games_completed"] + 1)
            logger.info("Planning %s week %s: %s/%s games collected; %s unique books requested", season, week,
                        self._status["games_completed"], len(games), len(books))
        return out

    async def run(self, season: int, week: int, now: datetime) -> dict:
        key = plan_key(self.cfg, season, week)
        existing = self.store.weekly_plan(key)
        if existing:
            return existing
        cfg, store = self.cfg, self.store
        store.record_strategy_version(cfg, now)
        engine = store.load_engine(cfg, self.fees)
        if engine.state.account_id is None:
            engine.open_account("paper-main", now)
        account = engine.account(now)
        equity = D(account["equity"])
        engine.open_week(season, week, now, equity)
        high_water = store.high_water(engine.state.starting_equity, engine.state.review_at)
        validation = store.latest_validation(cfg.version)
        validated = {k for k, v in ((validation or {}).get("payload", {}).get("buckets", {}) or {}).items()
                     if v.get("passed")}
        estimator = AnalogueEstimator(store.path_examples(), cfg)
        inputs = await self.candidates(season, week, now)
        self._report("evaluating", candidates=len(inputs), market_slug=None)
        decisions = []
        for inp in inputs:
            theta = engine.theta(now, inp.market_type) if inp.spec else ZERO
            decisions.append((inp, evaluate(inp, cfg, theta, estimator, validated), theta))
        # Portfolio selection: best conservative objective first, one directional trade per game.
        susp = suspension(engine, equity, high_water, season, week, cfg)
        expo = current_exposure(engine, lambda p: engine.theta(now, p.market_type))
        available = D(account["available_cash"])
        accepted = []
        ranked = sorted((x for x in decisions if any(c.partial_rating for c in x[1].combos)),
                        key=lambda x: -max(c.estimate.lower_return for c in x[1].combos if c.partial_rating))
        n = 0
        for inp, d, theta in ranked:
            if expo.games.get(inp.game["game_id"], 0) >= cfg.max_active_trades_per_game:
                d.reject("PORTFOLIO_LIMIT", "one active directional trade per game")
                continue
            trade_id = f"{season}W{week:02d}-{n + 1:02d}"
            finalize(d, inp, cfg, theta, equity=equity, available_cash=available, exposure=expo, suspended=susp,
                     trade_id=trade_id, plan_key=key)
            if d.accepted:
                n += 1
                p = d.plan
                reserve = engine.entry_reserve(p, now)
                available -= reserve
                games = dict(expo.games)
                games[p.game_id] = games.get(p.game_id, 0) + 1
                expo = Exposure(committed=expo.committed + reserve,
                                planned_loss=expo.planned_loss + p.planned_loss,
                                positions=expo.positions + 1, games=games)
                accepted.append((inp, d))
        summary = self._summary(decisions, accepted, account, validation, estimator)
        payload = {"plan_key": key, "season": season, "week": week, "as_of": iso(now), "config_version": cfg.version,
                   "rating_version": cfg.rating_version, "account_at_plan": account, "suspension": susp,
                   "trades": [d.plan.to_dict() for _, d in accepted],
                   "decisions": [d.to_dict(inp) for inp, d, _ in decisions]}
        outbox = [plan_publication(key, summary, payload, cfg)]
        outbox += [trade_card_publication(d.plan, "PROPOSED", None, cfg) for _, d in accepted]

        def write(db):
            db.execute("INSERT INTO weekly_plans VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (key, cfg.strategy_id, season, week, cfg.version, iso(now), "published", dumps(summary),
                        dumps(payload), iso(now)))
            for inp, d, _ in decisions:
                db.execute("INSERT INTO candidate_decisions (plan_key,game_id,market_slug,selected_outcome,accepted,"
                           "rejection_codes,payload,decided_at) VALUES (?,?,?,?,?,?,?,?)",
                           (key, d.game_id, d.market_slug, d.selected_outcome, int(d.accepted), dumps(d.codes),
                            dumps(d.to_dict(inp)), iso(now)))
                for v in (inp.gb, inp.mc):
                    if v.forecast_id:
                        db.execute("INSERT OR IGNORE INTO model_runs VALUES (?,?,?,?,?,?,?)",
                                   (v.forecast_id, v.provider, d.game_id, v.model_version, v.as_of, dumps(v.to_dict()),
                                    iso(now)))
            for _, d in accepted:
                db.execute("INSERT INTO trade_plans VALUES (?,?,?,?,?,?)",
                           (d.plan.trade_id, key, 1, "PROPOSED", dumps(d.plan.to_dict()), iso(now)))
                db.execute("INSERT INTO plan_revisions (plan_key,trade_id,revision,reason,payload,created_at) "
                           "VALUES (?,?,?,?,?,?)", (key, d.plan.trade_id, 1, "initial plan", dumps(d.plan.to_dict()),
                                                    iso(now)))
        store.commit(engine, now, outbox, extra=write)
        self._report("published", qualifying_trades=len(accepted))
        logger.info("Published paper plan %s: %s candidates, %s qualifying trades", key, len(inputs), len(accepted))
        return store.weekly_plan(key)

    def _summary(self, decisions, accepted, account, validation, estimator) -> dict:
        rejected = [d for _, d, _ in decisions if not d.accepted]
        primary = Counter(d.primary_code for d in rejected if d.primary_code)
        evidence = [d for d in rejected if d.evidence_limited]
        evaluated = [d for d in rejected if not d.evidence_limited]
        if accepted:
            headline = f"{len(accepted)} qualifying paper trade{'s' if len(accepted) != 1 else ''}."
        elif primary.get("INSUFFICIENT_EXECUTION_HISTORY"):
            # Some contracts cleared every model/market gate and stopped only on missing path evidence.
            headline = no_trade_text({"INSUFFICIENT_EXECUTION_HISTORY": 1})
        elif decisions and len(evidence) == len(rejected):
            headline = no_trade_text(Counter(d.primary_code for d in evidence))
        elif decisions:
            headline = "No good trades were found for this week."
        else:
            headline = "No upcoming games with listed contracts to evaluate."
        return {"headline": headline, "qualifying_trades": len(accepted), "candidates": len(decisions),
                "evaluated_unattractive": len(evaluated), "could_not_evaluate": len(evidence),
                "primary_reasons": dict(primary.most_common()), "equity": account["equity"],
                "available_cash": account["available_cash"],
                "validation_passed": bool(validation and validation["passed"]),
                "path_examples": len(estimator.examples)}

    async def activate(self, key: str, now: datetime) -> list[dict]:
        """Revalidate each PROPOSED trade on a fresh book; activate or invalidate. Idempotent."""
        plan = self.store.weekly_plan(key)
        if not plan:
            return []
        engine = self.store.load_engine(self.cfg, self.fees)
        results, outbox, status_updates = [], [], []
        for raw in plan["payload"]["trades"]:
            from .paper_engine import TradePlan
            p = TradePlan.from_dict(raw)
            if p.trade_id in engine.state.plans:
                continue
            row = self.store.trade_plan(p.trade_id)
            if row and row["status"] != "PROPOSED":
                continue
            reason = await self._revalidate(p, engine, now)
            if reason is None:
                res = engine.submit(p, now)
                reason = res["reason"]
            status = "WORKING" if reason is None else "INVALIDATED"
            status_updates.append((p.trade_id, status, reason))
            outbox.append(trade_card_publication(p, status, reason, self.cfg, revision=2))
            results.append({"trade_id": p.trade_id, "status": status, "reason": reason})

        def write(db):
            for tid, status, reason in status_updates:
                db.execute("UPDATE trade_plans SET status=? WHERE trade_id=?", (status, tid))
                if reason:
                    db.execute("INSERT OR IGNORE INTO plan_revisions (plan_key,trade_id,revision,reason,payload,"
                               "created_at) VALUES (?,?,?,?,?,?)", (key, tid, 2, f"invalidated: {reason}", "{}",
                                                                    iso(now)))
        self.store.commit(engine, now, outbox, extra=write)
        return results

    async def _revalidate(self, p, engine, now) -> str | None:
        if now >= p.entry_expires_at or p.game_id in engine.state.started_games:
            return "entry window closed"
        snap = await self.inputs.book(p.market_slug)
        if snap is None:
            return "no fresh book"
        fresh, why = is_fresh(snap, now, self.cfg.max_quote_age_seconds)
        if not fresh:
            return f"stale market data: {why}"
        view = selected_view(snap, p.selected_side)
        if view.best_bid is None or view.best_bid <= p.stop_trigger:
            return "best bid at or below stop trigger"
        account = engine.account(now)
        high_water = self.store.high_water(engine.state.starting_equity, engine.state.review_at)
        susp = suspension(engine, D(account["equity"]), high_water, p.season, p.week, self.cfg)
        if susp:
            return susp
        return None
