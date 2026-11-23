"""Read-mostly HTTP API for the paper account, consumed by the Discord bot.

Operator endpoints (pause/resume/review and the Discord outbox) require the
``X-Paper-Operator-Token`` header to equal ``NFL_PAPER_OPERATOR_TOKEN``. With no
token configured they are disabled.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import os

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from .common import utc
from .config import StrategyConfig
from .inputs import uncertainty_profiles_current
from .reports import CODE_TEXT, performance, trade_card
from .store import Store
from .worker import DB_PATH, HEALTH_PATH, trade_status

router = APIRouter(prefix="/api/paper", tags=["paper trading"])
_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(DB_PATH)
    return _store


def cfg() -> StrategyConfig:
    return StrategyConfig.from_env()


def now() -> datetime:
    return datetime.now(timezone.utc)


def operator_token() -> str | None:
    path = os.getenv("NFL_PAPER_OPERATOR_TOKEN_FILE")
    if path and os.path.exists(path):
        return open(path, encoding="utf-8-sig").read().strip() or None
    return os.getenv("NFL_PAPER_OPERATOR_TOKEN") or None


def operator(token: str | None):
    expected = operator_token()
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(403, "Operator token required")


@router.get("/parlay")
def parlay():
    card = store().latest_parlay()
    return {"card": card, "message": None if card else "A weekly parlay is not available yet."}


@router.get("/account")
def account():
    engine = store().load_engine(cfg())
    return {"mode": "PAPER_ONLY", **engine.account(now())}


@router.get("/picks")
def picks():
    s, c = store(), cfg()
    plan = s.latest_plan(c.strategy_id)
    if not plan:
        return {"plan": None, "trades": [], "message": "No weekly plan has been generated yet."}
    engine = s.load_engine(c)
    trades = [{**t, "status": trade_status(engine, t["trade_id"]) if t["trade_id"] in engine.state.plans
               else (s.trade_plan(t["trade_id"]) or {}).get("status", "PROPOSED")} for t in plan["payload"]["trades"]]
    return {"plan_key": plan["plan_key"], "season": plan["season"], "week": plan["week"], "as_of": plan["as_of"],
            "summary": plan["summary"], "trades": trades}


@router.get("/trades/{trade_id}")
def trade(trade_id: str):
    s, c = store(), cfg()
    row = s.trade_plan(trade_id)
    if not row:
        raise HTTPException(404, "Trade not found")
    engine = s.load_engine(c)
    plan = engine.state.plans.get(trade_id)
    status = trade_status(engine, trade_id) if plan else row["status"]
    return {"trade_id": trade_id, "status": status, "plan": row["payload"],
            "card": trade_card(plan or row["payload"], status), "revisions": row["revisions"],
            "history": row["events"]}


@router.get("/performance")
def perf():
    s = store()
    engine = s.load_engine(cfg())
    snaps = [r["payload"] for r in s.account_snapshots()]
    return {"account": engine.account(now()), "performance": performance(engine, snaps),
            "weekly_snapshots": [{"label": r["label"], "equity": r["equity"], "provisional": bool(r["provisional"])}
                                 for r in s.account_snapshots("weekly")]}


@router.get("/why-no-trades")
def why_no_trades():
    s, c = store(), cfg()
    plan = s.latest_plan(c.strategy_id)
    validation = s.latest_validation(c.version)
    readiness = {"path_examples": len(s.path_examples()),
                 "validation": None if not validation else {"passed": bool(validation["passed"]),
                                                            "created_at": validation["created_at"],
                                                            "reasons": validation["payload"].get("reasons")},
                 "uncertainty_profiles": uncertainty_profiles_current()}
    if not plan:
        return {"plan": None, "readiness": readiness}
    decisions = plan["payload"]["decisions"]
    gates = {}
    for d in decisions:
        for code in d["rejection_codes"]:
            gates[code] = gates.get(code, 0) + 1
    return {"plan_key": plan["plan_key"], "summary": plan["summary"], "readiness": readiness,
            "gate_failures": {k: {"count": v, "meaning": CODE_TEXT.get(k, k)} for k, v in
                              sorted(gates.items(), key=lambda x: -x[1])},
            "decisions": [{k: d[k] for k in ("game_id", "market_type", "selected_outcome", "primary_code",
                                             "evidence_limited", "notes")} for d in decisions]}


@router.get("/health")
def health():
    s = store()
    worker = json.loads(HEALTH_PATH.read_text()) if HEALTH_PATH.exists() else None
    engine = s.load_engine(cfg())
    feeds = engine.memory.get("last_received", {})
    t = now()
    plan = s.latest_plan(cfg().strategy_id)
    return {"mode": "PAPER_ONLY", "worker": worker,
            "feed_age_seconds": {k: (t - utc(v)).total_seconds() for k, v in feeds.items()},
            "pending_publications": len(s.pending_publications(100)),
            "last_planning_run": plan["as_of"] if plan else None, "paused": engine.state.paused,
            "outages": engine.state.outages[-5:]}


class OperatorAction(BaseModel):
    actor: str
    reason: str | None = None


@router.post("/pause")
def pause(body: OperatorAction, x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    s, t = store(), now()
    engine = s.load_engine(cfg())
    engine.pause(t, body.reason or "operator pause", body.actor)
    s.commit(engine, t)
    return {"paused": True, "note": "New entries paused. Exit monitoring and settlement continue."}


@router.post("/resume")
def resume(body: OperatorAction, x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    s, t = store(), now()
    engine = s.load_engine(cfg())
    engine.resume(t, body.actor)
    s.commit(engine, t)
    return {"paused": False}


@router.post("/review")
def review(body: OperatorAction, x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    s, t = store(), now()
    engine = s.load_engine(cfg())
    engine.record_review(t, engine.equity(t), body.reason or "strategy review", body.actor)
    s.commit(engine, t)
    return {"recorded": True}


@router.get("/outbox/pending")
def outbox(x_paper_operator_token: str | None = Header(None), channels: list[str] | None = Query(None),
           include_current_parlay: bool = False):
    operator(x_paper_operator_token)
    return {"publications": store().pending_publications(channels=channels, include_current_parlay=include_current_parlay)}


class Ack(BaseModel):
    revision: int
    message_id: str | None = None
    error: str | None = None


@router.post("/outbox/{publication_id}/ack")
def ack(publication_id: str, body: Ack, x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    store().ack_publication(publication_id, body.revision, body.message_id, body.error, now())
    return {"ok": True}


@router.get("/outbox/{publication_id}")
def publication(publication_id: str, x_paper_operator_token: str | None = Header(None)):
    operator(x_paper_operator_token)
    item = store().publication(publication_id)
    if not item:
        raise HTTPException(404, "Unknown publication")
    return item
