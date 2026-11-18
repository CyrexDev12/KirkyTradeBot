# NFL paper trading (PAPER_ONLY)

A persistent $100 **paper** account that plans NFL contract-price trades from the two existing sports models, simulates execution against recorded Polymarket US order books, and reports results in Discord. It reads public market data only. It never places, cancels, or changes a real exchange order (`backend/trading/venue.py` refuses any non-GET or non-allow-listed route, and a test scans the backend for order-route code).

Every numeric strategy setting is an initial experiment setting. None of them is an optimized parameter or evidence of profitability. A positive paper return is not an acceptance criterion.

## What runs where

| Piece | Module | Role |
| --- | --- | --- |
| Venue adapter | `trading/venue.py`, `book.py` | Read-only GETs, pacing, 429/5xx backoff, book parsing, YES/NO normalization |
| Contracts | `trading/contracts.py` | Moneyline and half-point full-game total mapping, orientation, settlement rules, increments |
| Valuation | `trading/valuation.py` | Separate GB and MC payout values with documented lower bounds. Never averaged; MC never replaced by GB |
| Paper engine | `trading/paper_engine.py` | Event-sourced orders, fills, exits, settlement, marks |
| Ledger / store | `trading/store.py` | SQLite journal, materialized orders/fills/lots/reservations, outbox, snapshots |
| Risk | `trading/risk.py` | Sizing ceilings, exposure, drawdown suspensions |
| Path model | `trading/path_model.py` | Replay labels through the same engine, censoring, analogue estimator, validation |
| Screening | `trading/candidates.py` | Hard gates, rejection codes, frozen grid, final-size recheck |
| Rating | `trading/scoring.py` | Versioned 1–10 index with absolute normalization |
| Planner / jobs | `trading/planner.py`, `jobs.py` | One plan per week, revalidation, slate detection, Tuesday fallback |
| Worker | `trading/worker.py` | Persistent loop: record books, run the engine, run weekly jobs |
| API | `trading/api.py` | `/api/paper/*`, mounted in `backend.app` |
| Discord | `bot/paper.py` | Commands, channel routing, outbox delivery with reconciliation and in-place edits |

## Setup

1. Create the shared operator secret used by the backend and the bot:
   ```bash
   mkdir -p .secrets && openssl rand -hex 32 > .secrets/paper_operator_token.txt
   ```
   The Azure deploy scripts create it on the VM if it is missing.
2. Start everything: `docker compose up --build -d`. This adds a `paper-worker` service that shares the `predictions` volume. The paper database is `/data/trading/paper_trading.sqlite3`. The account opens once at $100 and is never reset or replenished.
3. Build the documented model-uncertainty profiles. Without them, every candidate is rejected with `UNSUPPORTED_UNCERTAINTY`:
   ```bash
   python -m backend.trading.cli uncertainty --mc-seasons 2024,2025,2026
   ```
   GB moneyline uses the existing walk-forward calibration bins. MC uses `mc_validation` reconstructions for the seasons you list. The current metrics have no GB total-line calibration, so GB totals stay unsupported.
4. In Discord (Manage Server), route posts: `/paper-channels set kind:picks|updates|reports channel:#…`.

Local, without Docker:
```bash
export NFL_PAPER_DB=data/trading/paper_trading.sqlite3 NFL_PAPER_OPERATOR_TOKEN=$(cat .secrets/paper_operator_token.txt)
python -m uvicorn backend.app:app --port 8000      # API incl. /api/paper
python -m backend.trading.worker                    # recorder + engine + weekly jobs
python -m bot.discord_bot                           # bot (also needs NFL_PAPER_OPERATOR_TOKEN)
```

## Discord commands

`/picks`, `/trade <id>`, `/account`, `/performance`, `/why-no-trades`, `/health`. Operators (Manage Server) also get `/paper-pause`, `/paper-resume` and `/paper-channels set`. Pausing stops new entries only. Exit monitoring, deadlines and settlement keep running.

## Weekly flow

1. The worker sees every game of the previous week final (or canceled) and waits 30 minutes.
2. It settles open paper inventory from final scores under the plan's settlement rule, saves an immutable account snapshot, and publishes the weekly report. This step does not wait for NFL statistics.
3. When the GB predictions artifact covers the next week and was refreshed after the slate, it builds the plan. The plan uses a fixed as-of snapshot, both models, verified contracts and fresh books, and stores every decision and rejection reason. It then publishes the plan, or a no-trade result.
4. Each accepted trade is revalidated against a fresh book before its local entry activates. Planning and activation are idempotent by `strategy:season:week`.
5. If data are late, a delayed-status post goes out at 08:00 America/Phoenix on Tuesday and the job retries.

## Execution rules (conservative observable-book mode)

- Entries fill only after a 2 s activation latency, on fresh open books, only against selected-side asks at or below the limit, up to 50% of displayed size. Fills are booked at the limit price, with no assumed price improvement, and pay taker fees.
- A price level that supplied a fill counts as spent. It can supply more only after it shows a larger displayed size, and only the increase can be used.
- Targets fill only against bids at or above the target, booked at the target price. The first target fill cancels any remaining entry quantity, so a profit is never followed by re-entry.
- Stops trigger when the fresh best selected bid is at or below the trigger. Stop and time exits walk the bids at their actual prices. A gap is booked at the levels that actually trade. With no bids, the position stays open.
- Halted or closed markets, stale books and feed gaps never produce fills. Gaps are journaled as outages.
- Holding profiles:
  - `FROZEN_THESIS_PRICE_EXITS` (default): entries expire 10 min before kickoff, and price exits keep running until kickoff + 4 h. NFL forecasts are frozen at kickoff, and no new in-game positions are opened.
  - `PREGAME_ONLY`: liquidation starts 5 min before kickoff.
- Fees use `theta × contracts × p × (1−p)` with θ = 0.095 (taker, lowest-volume tier, effective 2026-11-14). Each fill is rounded to the cent, and the total is capped at the rounded sum. No promotional coefficient and no rebates.

## Why the first weeks will publish zero trades

The project has no timestamped historical Polymarket quotes. A rated trade needs a market-path estimate for its bucket (market type, price band, target/stop distance, holding profile). That estimate needs at least 30 games, 6 weeks and 30 uncensored filled paths, and no more than 10% censored. The trade also needs a passing chronological validation artifact for the bucket. Until then the honest result is:

> No rated trades this week — insufficient market-path history to estimate scalp outcomes.

The worker starts recording books right away. Then run:

```bash
python -m backend.trading.cli label-history   # replay recorded books through the same engine → labeled paths
python -m backend.trading.cli validate        # chronological holdout + sensitivity artifact
```

Censored paths are kept. They count in coverage, the point estimate books them at the stressed-stop loss, and the lower bound books them at the full funded loss. Official price-history display prices are stored separately and never used for fills or labels.

## Rating, gates and sizing

- **Gates** run in order with explicit codes: `NO_LISTED_MARKET`, `CONTRACT_MAPPING_FAILED`, `UNSUPPORTED_SETTLEMENT`, `MARKET_NOT_OPEN`, `MC_UNAVAILABLE`, `GB_UNAVAILABLE`, `UNSUPPORTED_UNCERTAINTY`, `STALE_MARKET_DATA`, `UNKNOWN_INCREMENTS`, `INSUFFICIENT_LIQUIDITY`, `NO_MODEL_CONSENSUS`, `PLAN_INVALID_FOR_BOOK`, `INSUFFICIENT_EXECUTION_HISTORY`, `NEGATIVE_NET_EXPECTANCY`, `ENTRIES_SUSPENDED`, `PORTFOLIO_LIMIT`, `RATING_UNAVAILABLE`, `RATING_BELOW_MINIMUM`.
- **Valuation screen:** `min(GB_lower, MC_lower) − all-in entry cost ≥ 0.02`. It is checked on the reference grid and again at the final size.
- **Rating:** `round(1 + 9·(0.35a + 0.25b + 0.20c + 0.10d + 0.10e), 1)`. It uses absolute normalization (`rating-v1-dev-defaults`) and never percentile ranks. Missing evidence means no rating. The rating is not a probability of profit.
- **Sizing ceilings:**
  - Stressed-stop loss per trade: 1%, 2% or 3% of equity by rating tier.
  - Funded loss per game: 15% of equity.
  - Committed cost, including working entries: 40% of equity.
  - Aggregate stressed loss: 5% of equity.
  - At most 3 positions, and 1 per game.
  - New entries are suspended after an 8% weekly drawdown, or a 20% drawdown from high water until a review is recorded.

## Capability report

`python -m backend.trading.cli capabilities` prints the current list.

- **Implemented and tested:** everything in the table above, plus the 35 tests in `tests/test_paper_trading.py`, which cover every case in the brief's section 18.
- **Collecting data:** book snapshots and market-path examples.
- **Not verified against live endpoints:**
  - Polymarket US field names for tick size, quantity step, market states and the settlement schema. Contracts with unknown increments are rejected with `UNKNOWN_INCREMENTS`.
  - Exact fee aggregation beyond the dashboard convention.
- **Unsupported:**
  - Passive queue fills.
  - Authenticated WebSocket streaming.
  - GB total-line uncertainty.
  - Integer total lines.
  - Props and period or team totals.
  - Postseason MC cases.
  - Fractional Kelly sizing.
  - Real orders, by design.

See `docs/PAPER_TRADING_MOCK_RUN.md` for a worked run. It covers partial entry fills, once-only liquidity, a partial target exit, a stop that gaps from 11¢ to 7¢/6¢, an expired entry, settlement, and a reconciliation of journal cash against the account after a full rebuild.
