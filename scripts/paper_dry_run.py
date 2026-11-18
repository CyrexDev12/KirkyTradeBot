"""End-to-end paper-trading dry run without a Discord token.

    NFL_DATA_DIR=/tmp/nfl NFL_PAPER_DB=/tmp/nfl/paper.sqlite3 python -m backend.prepare
    NFL_DATA_DIR=/tmp/nfl NFL_PAPER_DB=/tmp/nfl/paper.sqlite3 python -m backend.trading.cli uncertainty --mc-seasons 2024,2025,2026
    NFL_DATA_DIR=/tmp/nfl NFL_PAPER_DB=/tmp/nfl/paper.sqlite3 python scripts/paper_dry_run.py [capture.json]

Use a scratch NFL_DATA_DIR/NFL_PAPER_DB: the run opens a paper account and stores a weekly plan.

Real: NFLverse schedule, the trained GB/totals models, the current-season MC,
the paper engine/planner/jobs/worker, the FastAPI /api/paper routes, and the
bot's publisher + formatting code.
Substituted: the Polymarket network (books replayed from a live capture taken
through the user's browser, served as if freshly polled) and the Discord
channel (prints each post instead of sending it).
"""
import asyncio
import pathlib
from datetime import datetime, timezone
import json
import os
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
CAPTURE = sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parents[1] / "tests/fixtures/polymarket_capture_2026-11-18.json")
os.environ.setdefault("NFL_PAPER_OPERATOR_TOKEN", "dry-run-token")
os.environ.setdefault("NFL_PAPER_POSTGAME_INGESTION_GRACE_MINUTES", "0")  # do not wait 30 min in a test

import httpx  # noqa: E402

from backend.trading.book import parse_book  # noqa: E402
from backend.trading.config import StrategyConfig  # noqa: E402
from backend.trading.inputs import LiveInputs  # noqa: E402
from backend.trading.store import Store  # noqa: E402
from backend.trading.worker import DB_PATH, Worker  # noqa: E402

cap = json.load(open(CAPTURE))
ML, TOT = cap["market_defaults"]["moneyline"], cap["market_defaults"]["total"]


def ml_market(ev):
    _, start, away, home, slug, _, _ = ev
    return {"id": slug, "slug": slug, "active": True, "closed": False, "marketType": "moneyline",
            "sportsMarketType": "football_team_full_game_winner", **ML,
            "marketSides": [{"long": True, "team": {"abbreviation": away, "ordering": "away"}},
                            {"long": False, "team": {"abbreviation": home, "ordering": "home"}}]}


def total_market(ev):
    _, _, away, home, _, slug, line = ev
    return {"id": slug, "slug": slug, "line": line, "active": True, "closed": False, "marketType": "totals",
            "sportsMarketType": "football_team_full_game_total",
            "orderPriceMinTickSize": TOT["orderPriceMinTickSize"], "minimumTradeQty": TOT["minimumTradeQty"],
            "feeCoefficient": TOT["feeCoefficient"],
            "description": f"This market will settle to Yes if {away.upper()} and {home.upper()} combine for over "
                           f"{line} points in the professional football game. Overtime is included if played.",
            "marketSides": [{"long": True, "description": "Over"}, {"long": False, "description": "Under"}]}


class ReplayVenue:
    """Read-only stand-in for ReadOnlyVenueClient that replays the capture."""
    last_error = None

    def clock(self):
        return datetime.now(timezone.utc)

    async def get(self, path, params=None):
        if path == "/v2/leagues/nfl/events":
            if (params or {}).get("offset", 0):
                return {"events": []}
            return {"events": [{"id": e[0], "startTime": e[1], "teams": [{"abbreviation": e[2]}, {"abbreviation": e[3]}],
                                "markets": [ml_market(e)]} for e in cap["events"]]}
        if path.startswith("/v1/events/"):
            ev = next(e for e in cap["events"] if e[0] == path.rsplit("/", 1)[1])
            return {"event": {"id": ev[0], "startTime": ev[1], "teams": [{"abbreviation": ev[2]}, {"abbreviation": ev[3]}],
                              "markets": [ml_market(ev), total_market(ev)]}}
        raise AssertionError(f"unexpected route {path}")

    async def book(self, slug):
        transact, bids, offers = cap["books"][slug]
        payload = {"marketData": {"state": "MARKET_STATE_OPEN", "transactTime": transact,
                                  "bids": [{"px": {"value": p}, "qty": q} for p, q in bids],
                                  "offers": [{"px": {"value": p}, "qty": q} for p, q in offers]}}
        return parse_book(slug, payload, self.clock())

    async def close(self):
        pass


class PrintChannel:
    def __init__(self):
        self.n = 0

    async def find_existing(self, channel_id, pid, started_at):
        return None

    async def send(self, channel_id, embed, nonce):
        self.n += 1
        name = {1: "#paper-picks", 2: "#paper-updates", 3: "#paper-reports"}[channel_id]
        print(f"\n┌─ Discord post → {name}\n│ **{embed.title}**")
        for line in (embed.description or "").splitlines():
            print(f"│ {line}")
        for f in embed.fields:
            print(f"│ [{f.name}] {f.value}")
        print(f"│ ({embed.footer.text})\n└─")
        return 1000 + self.n

    async def edit(self, channel_id, message_id, embed):
        print(f"(edit message {message_id}: {embed.title})")


async def main():
    cfg = StrategyConfig.from_env()
    store = Store(DB_PATH)
    venue = ReplayVenue()
    worker = Worker(store, cfg, venue, LiveInputs(venue, store))
    now = datetime.now(timezone.utc)
    if worker.engine.state.account_id is None:
        worker.engine.open_account("paper-main", now)
        store.commit(worker.engine, now)
    print("== Worker loop 1 (slate detection, weekly report, weekly plan, activation) ==")
    await worker.loop_once()
    print("worker status:", {k: v for k, v in worker.status.items() if k != "feed_errors"})
    print("== Worker loop 2 (records candidate books, runs engine/clock) ==")
    worker.last_jobs = 0
    await worker.loop_once()

    plan = store.latest_plan(cfg.strategy_id)
    print("\n== Weekly plan decisions (live books, real models) ==")
    rows = []
    for d in plan["payload"]["decisions"]:
        gb, mc = d.get("valuations", {}).get("GB", {}), d.get("valuations", {}).get("MC", {})
        rows.append((d["game_id"], d["market_type"], d["selected_outcome"], gb.get("fair_value"), gb.get("lower"),
                     mc.get("fair_value"), mc.get("lower"), d["primary_code"]))
    print(f"{'game':18} {'mkt':9} {'side':6} {'GB fv':>7} {'GB lo':>7} {'MC fv':>7} {'MC lo':>7}  primary reason")
    for r in rows:
        f = lambda x: f"{float(x):.3f}" if x not in (None, "None") else "  —  "
        print(f"{r[0]:18} {r[1]:9} {r[2]:6} {f(r[3]):>7} {f(r[4]):>7} {f(r[5]):>7} {f(r[6]):>7}  {r[7]}")
    print("\nsummary:", json.dumps(plan["summary"], indent=1))

    print("\n== Discord delivery through the real bot publisher and /api/paper (no token) ==")
    from backend import app as backend_app
    from backend.trading import api as paper_api
    from bot.api import PredictionAPI
    from bot.paper import PaperPosts, PaperPublisher, account_text
    paper_api._store = store
    client = PredictionAPI("http://dry-run", token=os.environ["NFL_PAPER_OPERATOR_TOKEN"])
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=backend_app.app), base_url="http://dry-run")
    posts = PaperPosts(os.path.join(os.path.dirname(DB_PATH), "bot_state.sqlite3"))
    for kind, channel in (("picks", 1), ("updates", 2), ("reports", 3)):
        posts.set_channel(99, kind, channel)
    channel = PrintChannel()
    await PaperPublisher(client, posts, channel).tick()
    print(f"\n{channel.n} posts delivered; pending after delivery:",
          len((await client.paper_outbox())["publications"]))
    await PaperPublisher(client, posts, channel).tick()
    print("second publisher tick (duplicate check) sent:", channel.n, "total")

    print("\n== /account ==")
    print(account_text(await client.paper("/account")))
    why = await client.paper("/why-no-trades")
    print("\n== /why-no-trades ==")
    print(why["summary"]["headline"])
    print("readiness:", why["readiness"])
    for code, info in why["gate_failures"].items():
        print(f"  {code} x{info['count']} — {info['meaning']}")
    print("\n== /health ==")
    h = await client.paper("/health")
    print({k: h[k] for k in ("mode", "pending_publications", "last_planning_run", "paused")},
          "markets with recorded books:", len(h["feed_age_seconds"]))
    await client.close()


asyncio.run(main())
