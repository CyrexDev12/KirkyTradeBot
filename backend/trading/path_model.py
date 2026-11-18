"""Market-path labels, a historical-analogue estimator and the readiness gate.

The question estimated here is the trade's, not the game's: *conditional on
this entry actually filling, how likely is the position to close at its target
before its stop, time limit or settlement, and what net cash flows follow?*

Labels are produced by replaying recorded books through the SAME paper engine
and configuration the live paper bot uses, so a label can only say "target"
when the conservative fill rules would actually have sold the inventory.

Label precedence (mutually exclusive, highest first):
CENSORED > NO_FILL > MIXED > TARGET | STOP | TIME | SETTLEMENT.
A path is CENSORED when an unobserved interval longer than
``max_history_interval_seconds`` occurs while an order or inventory is live, a
feed outage is journaled, or the replay ends with the outcome unresolved.
Censored paths are never scored as zero-return trades and never silently
dropped: they count in coverage, the point estimate books them at the planned
stressed-stop loss, and the lower bound books them at the full funded loss.
When too many are censored the estimate is withheld.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
import random

from .common import ZERO, D, iso, utc
from .config import StrategyConfig
from .fees import DEFAULT_PROFILES, FeeProvider
from .paper_engine import PaperEngine, TradePlan

CENSORED, NO_FILL, MIXED = "CENSORED", "NO_FILL", "MIXED"
EXIT_LABELS = ("TARGET", "STOP", "TIME", "SETTLEMENT")


def bucket(market_type: str, hold_policy: str, entry: Decimal, target: Decimal, stop: Decimal) -> str:
    """Analogue bucket: market type, holding profile, 5-cent entry band, target/stop distance in cents."""
    entry_band = (entry / Decimal("0.05")).to_integral_value(ROUND_FLOOR) * Decimal("0.05")
    t = ((target - entry) * 100).quantize(Decimal("0.01"))
    s = ((entry - stop) * 100).quantize(Decimal("0.01"))
    return f"{market_type}|{hold_policy}|e{entry_band:.2f}|t{t.normalize()}c|s{s.normalize()}c"


def label_path(plan: TradePlan, snapshots, cfg: StrategyConfig, settlement_payout: Decimal | None = None,
               fees: FeeProvider | None = None) -> dict:
    engine = PaperEngine(cfg, fees)
    start = plan.entry_active_after
    engine.open_account("replay", start - timedelta(seconds=1), D("1000000"))
    result = engine.submit(plan, start)
    if not result["accepted"]:
        raise ValueError(f"Replay could not activate plan: {result['reason']}")
    last = start
    censored_reason = None
    for snap in snapshots:
        if snap.received_at < start:
            continue
        live = _live(engine, plan.trade_id)
        if not live:
            break
        if (snap.received_at - last).total_seconds() > cfg.max_history_interval_seconds:
            censored_reason = "unobserved interval while live"
            break
        engine.on_snapshot(snap)
        if any(e["type"] == "OUTAGE" for e in engine._pending):
            censored_reason = "feed outage"
            break
        last = snap.received_at
    if censored_reason is None and _live(engine, plan.trade_id):
        # The recorded path must reach each deadline it relies on; otherwise the outcome was not observed.
        o = engine.state.orders.get(plan.trade_id)
        entry_live = o is not None and o.status in ("WORKING", "PARTIALLY_FILLED")
        horizon = plan.entry_expires_at if entry_live else plan.exit_deadline
        if (horizon - last).total_seconds() > cfg.max_history_interval_seconds:
            censored_reason = "recorded path ends before the deadline"
    if censored_reason is None:
        engine.on_clock(max(last, plan.entry_expires_at))
        if _live(engine, plan.trade_id):
            if settlement_payout is not None and plan.trade_id in engine.state.positions:
                engine.on_settlement(plan.market_slug, {plan.trade_id: settlement_payout}, last, "replay")
            if _live(engine, plan.trade_id):
                censored_reason = "unresolved at end of recorded path"
    pos = engine.state.positions.get(plan.trade_id)
    filled = pos.qty_bought if pos else ZERO
    if censored_reason:
        label = CENSORED
    elif filled <= 0:
        label = NO_FILL
    else:
        reasons = [r for r, q in pos.exit_qty.items() if D(q) > 0]
        label = reasons[0] if len(reasons) == 1 else MIXED
    committed = pos.total_cost if pos else ZERO
    realized = pos.realized if pos and not censored_reason else None
    return {"trade_id": plan.trade_id, "game_id": plan.game_id, "season": plan.season, "week": plan.week,
            "bucket": bucket(plan.market_type, plan.hold_policy, plan.entry_limit, plan.target_limit,
                             plan.stop_trigger),
            "label": label, "censored_reason": censored_reason, "filled_quantity": str(filled),
            "planned_quantity": str(plan.quantity), "committed": str(committed),
            "realized": str(realized) if realized is not None else None,
            "return": str(realized / committed) if realized is not None and committed > 0 else None,
            "stress_floor_return": str(-plan.planned_loss / plan.estimated_entry_cost),
            "decision_at": iso(plan.decision_at), "config_version": cfg.version, "origin": "replay"}


def _live(engine, trade_id) -> bool:
    o = engine.state.orders.get(trade_id)
    pos = engine.state.positions.get(trade_id)
    return bool((o and o.status in ("WORKING", "PARTIALLY_FILLED")) or (pos and pos.qty_open > 0))


def stressed_variants(cfg: StrategyConfig) -> dict:
    """Sensitivity configurations: more latency, thinner participation, higher fees."""
    base_fee = DEFAULT_PROFILES[0]
    higher = replace(base_fee, profile_id=base_fee.profile_id + "-stress", taker_theta=base_fee.taker_theta * Decimal("1.25"))
    return {
        "latency+5s": (cfg.with_overrides(latency_seconds=cfg.latency_seconds + 5), None),
        "participation/2": (cfg.with_overrides(entry_participation_cap=cfg.entry_participation_cap / 2), None),
        "fees*1.25": (cfg.with_overrides(fee_profile_id=higher.profile_id), FeeProvider((higher,), higher.profile_id)),
    }


@dataclass
class PathEstimate:
    status: str  # "supported" | "insufficient"
    reasons: list = field(default_factory=list)
    bucket: str | None = None
    games: int = 0
    weeks: int = 0
    examples: int = 0
    filled: int = 0
    censored_fraction: Decimal | None = None
    fill_probability: Decimal | None = None
    state_probabilities: dict = field(default_factory=dict)  # conditional on fill
    target_interval: tuple | None = None
    expected_return: Decimal | None = None  # net P&L per committed dollar | fill
    lower_return: Decimal | None = None

    @property
    def supported(self) -> bool:
        return self.status == "supported"

    def to_dict(self):
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = str(v)
        d["state_probabilities"] = {k: str(v) for k, v in self.state_probabilities.items()}
        d["target_interval"] = [str(x) for x in self.target_interval] if self.target_interval else None
        return d


class AnalogueEstimator:
    """Bucketed historical analogues with a game-clustered bootstrap."""

    def __init__(self, examples: list[dict], cfg: StrategyConfig, seed: int = 17):
        self.examples, self.cfg, self.seed = examples, cfg, seed

    def estimate(self, bucket_key: str, before: datetime | None = None) -> PathEstimate:
        cfg = self.cfg
        rows = [x for x in self.examples if x["bucket"] == bucket_key
                and (before is None or utc(x["decision_at"]) < before)]
        est = PathEstimate(status="insufficient", bucket=bucket_key, examples=len(rows))
        est.games = len({x["game_id"] for x in rows})
        est.weeks = len({(x["season"], x["week"]) for x in rows})
        filled = [x for x in rows if x["label"] not in (NO_FILL,)]
        est.filled = sum(1 for x in filled if x["label"] != CENSORED)
        censored = [x for x in rows if x["label"] == CENSORED]
        est.censored_fraction = D(len(censored)) / D(len(rows)) if rows else None
        if not rows:
            est.reasons.append("no recorded market-path analogues for this market, price regime and horizon")
            return est
        if est.games < cfg.readiness_min_games:
            est.reasons.append(f"{est.games} distinct games < {cfg.readiness_min_games}")
        if est.weeks < cfg.readiness_min_weeks:
            est.reasons.append(f"{est.weeks} distinct weeks < {cfg.readiness_min_weeks}")
        if est.filled < cfg.readiness_min_filled:
            est.reasons.append(f"{est.filled} uncensored filled paths < {cfg.readiness_min_filled}")
        if est.censored_fraction > cfg.readiness_max_censored_fraction:
            est.reasons.append(f"censored fraction {est.censored_fraction:.2f} > {cfg.readiness_max_censored_fraction}")
        decided = [x for x in rows if x["label"] != CENSORED]
        est.fill_probability = (D(sum(1 for x in decided if x["label"] != NO_FILL)) / D(len(decided))) if decided else None
        if filled:
            n = D(len(filled))
            for state in (*EXIT_LABELS, MIXED, CENSORED):
                est.state_probabilities[state] = D(sum(1 for x in filled if x["label"] == state)) / n
            est.expected_return = self._mean(filled, lower=False)
        if est.reasons:
            return est
        draws = self._bootstrap(filled)
        q = cfg.readiness_lower_quantile
        returns = sorted(r for r, _ in draws)
        targets = sorted(t for _, t in draws)
        idx = int((len(returns) - 1) * float(q))
        est.lower_return = returns[idx]
        est.target_interval = (targets[idx], targets[len(targets) - 1 - idx])
        est.status = "supported"
        return est

    @staticmethod
    def _value(x: dict, lower: bool) -> Decimal:
        if x["label"] == CENSORED:
            return D("-1") if lower else D(x["stress_floor_return"])
        return D(x["return"])

    def _mean(self, rows, lower: bool) -> Decimal:
        return sum((self._value(x, lower) for x in rows), ZERO) / D(len(rows))

    def _bootstrap(self, filled):
        rng = random.Random(self.seed)
        by_game: dict[str, list] = {}
        for x in filled:
            by_game.setdefault(x["game_id"], []).append(x)
        games = sorted(by_game)
        out = []
        for _ in range(self.cfg.readiness_bootstrap_draws):
            sample = [x for g in (rng.choice(games) for _ in games) for x in by_game[g]]
            hits = D(sum(1 for x in sample if x["label"] == "TARGET")) / D(len(sample))
            out.append((self._mean(sample, lower=True), hits))
        return out


def validate(examples: list[dict], cfg: StrategyConfig, eval_fraction: Decimal = Decimal("0.3"),
             sensitivity: dict | None = None) -> dict:
    """Chronological walk-forward check by NFL week. Returns a machine-readable artifact.

    The evaluation block is the latest weeks and is never used to fit. Any
    example whose decision falls in the evaluation block is excluded from the
    development fit (game/week grouping keeps all snapshots of a game together).
    """
    weeks = sorted({(x["season"], x["week"]) for x in examples})
    artifact = {"strategy_version": cfg.version, "method": "chronological-week-holdout-v1", "weeks": len(weeks),
                "examples": len(examples), "buckets": {}, "passed": False, "reasons": []}
    if len(weeks) < cfg.readiness_min_weeks + 2:
        artifact["reasons"].append("not enough distinct weeks for a development block and an untouched evaluation block")
        return artifact
    cut = max(1, int(len(weeks) * float(eval_fraction)))
    eval_weeks = set(weeks[-cut:])
    dev = [x for x in examples if (x["season"], x["week"]) not in eval_weeks]
    held = [x for x in examples if (x["season"], x["week"]) in eval_weeks]
    est = AnalogueEstimator(dev, cfg)
    any_pass = False
    for key in sorted({x["bucket"] for x in examples}):
        e = est.estimate(key)
        h = [x for x in held if x["bucket"] == key and x["label"] not in (NO_FILL,)]
        observed = (sum((AnalogueEstimator._value(x, True) for x in h), ZERO) / D(len(h))) if h else None
        hits = (D(sum(1 for x in h if x["label"] == "TARGET")) / D(len(h))) if h else None
        entry = {"development": e.to_dict(), "evaluation_filled": len(h),
                 "evaluation_games": len({x["game_id"] for x in h}),
                 "evaluation_conservative_mean_return": str(observed) if observed is not None else None,
                 "evaluation_target_rate": str(hits) if hits is not None else None, "passed": False, "reasons": []}
        if not e.supported:
            entry["reasons"] += e.reasons
        if entry["evaluation_games"] < 10:
            entry["reasons"].append("fewer than 10 evaluation games")
        if observed is not None and observed <= 0:
            entry["reasons"].append("non-positive conservative return in evaluation block")
        if e.supported and e.lower_return is not None and e.lower_return <= 0:
            entry["reasons"].append("non-positive development lower bound")
        for name, rows in (sensitivity or {}).items():
            srows = [x for x in rows if x["bucket"] == key and (x["season"], x["week"]) not in eval_weeks]
            s_est = AnalogueEstimator(srows, cfg).estimate(key)
            entry.setdefault("sensitivity", {})[name] = s_est.to_dict()
            if not s_est.supported or (s_est.lower_return or ZERO) <= 0:
                entry["reasons"].append(f"fails sensitivity {name}")
        entry["passed"] = not entry["reasons"]
        any_pass |= entry["passed"]
        artifact["buckets"][key] = entry
    artifact["passed"] = any_pass
    if not any_pass:
        artifact["reasons"].append("no market/horizon bucket passed every validation requirement")
    return artifact
