# NFL Fair Value

Local, read-only NFL pregame analytics dashboard. It estimates each team's full-game win probability and the combined final score from NFLverse history, then compares those estimates with live [Polymarket US](https://docs.polymarket.us/data-guide/overview) moneyline and game-total contracts. It does not place trades.

## Run

Requires Python 3.11 or newer. From this directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m backend.prepare
.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Initial preparation downloads NFLverse schedules and play-by-play from 2015 through the current season, aggregates team-game metrics locally, fits the model, and writes validation results. Subsequent starts use the local `data/` cache. The server checks for a model refresh every 30 minutes and rebuilds when predictions are more than six hours old. The current-season NFL data cache expires after six hours. Quotes refresh in the browser every 45 seconds, with a manual refresh button.

For a forced data rebuild, run `.venv/bin/python -m backend.prepare --refresh`. The `data/` directory is ignored by Git; it contains downloaded NFLverse aggregates, predictions, metrics, and a model pickle. Treat the pickle as local trusted output and do not load untrusted replacements.

## Model and data integrity

- Schedule and play-by-play data come from [`nflreadpy`](https://nflreadpy.nflverse.com/api/load_functions/). The pipeline stores per-team, per-game EPA, success rate, passing and rushing EPA, explosive rates, defensive equivalents, QB dropback EPA, and CPOE when present. It also derives recent form, records, opponent-strength history, rest difference, and a season-regressed Elo baseline.
- Each game's features are captured before its result and play-by-play enter the team history. Completed games are processed chronologically. Current games with no final score never update the historical features.
- The win-probability model is a deterministic histogram gradient-boosted classifier. It has no Polymarket price input. For each walk-forward season, a logistic calibration mapping is fit only on earlier out-of-sample seasons; the production mapping is fit on past seasons. Home and away probabilities sum to one. Ties are rare and excluded from binary model training; market tie settlement is shown in game details.
- A separate histogram gradient-boosted regression model predicts combined final points, including overtime. It uses only pregame rolling team points scored and allowed, offensive and defensive efficiency, recent scoring, play volume, rest, week, and season. Its over/under probability at each listed line comes from the empirical distribution of prior-season out-of-sample score errors. The 80% interval is built from those errors. Exchange prices never enter this model.
- Validation is season walk-forward: each test season is predicted by a model trained on earlier seasons. Historical predictions are labeled **reconstructions**, because they were not contemporaneously archived. The validation endpoint reports Brier score, log loss, accuracy, season breakdowns, and calibration bins. Trading performance is intentionally absent without real timestamped historical quotes.
- Predictions generated before kickoff are cached and frozen after kickoff. Market prices are never used to retrain the model. If data or model artifacts are unavailable, the UI shows an error or empty value instead of a substitute estimate.

The initial win-model run in this workspace produced 1,420 prior-season out-of-sample games with a Brier score of 0.2291, log loss of 0.6502, and accuracy of 0.6387. The total-points model's 2021–2025 walk-forward evaluation covered 1,424 games with MAE 10.8 points, RMSE 13.59 points, and 82.3% coverage of its nominal 80% interval. Rebuilds can change these values as NFLverse data is updated.

## Market matching and valuation

The app queries the public [NFL league events endpoint](https://docs.polymarket.us/api-reference/sports/get-events-by-league-slug) and verifies exact home and away team identifiers and roles, kickoff time, the `football_team_full_game_winner` market type, moneyline status, and settlement language. It then retrieves each matched [event detail](https://docs.polymarket.us/api-reference/events/get-event-by-id) and selects only active `football_team_full_game_total` combined-score, full-game, half-point lines with explicit Over and Under sides. Team totals, period totals, spreads, and props are excluded. Unverified games stay on the schedule with **Market Unavailable**.

The board features the total line whose exchange display price is closest to 50%, and matchup details list every verified full-game total line. Over is the long contract and Under is the short contract. The featured line uses a live order book for quantity-aware net edge; the rest of the ladder uses current event reference quotes and shows model-versus-market differences without an executable edge. If the exchange has no total contract for a game, the projected score still appears.

Polymarket US represents a two-team winner market with one long/short instrument. To buy the long team's outcome, the executable starting price is the best ask. To buy the other team's outcome, it is `1 − best bid`. The dashboard uses the full [order book](https://docs.polymarket.us/api-reference/markets/get-market-book) to compute a quantity-weighted purchase price for the selected trade size. A signal is suppressed when the depth is insufficient, the market is closed, the fee coefficient is missing, or a retained quote is stale.

Net edge is model fair value minus book execution cost minus estimated taker fees. Fee estimates use each consumed book level and the market's current coefficient under the [Polymarket US fee formula](https://docs.polymarket.us/fees): `coefficient × contracts × price × (1 − price)`. Actual fees can differ if orders split across resting fills or the user's fee tier differs. The dashboard displays estimated value rather than a recommendation or a demonstrated profitable strategy. Thresholds can be set with `NFL_MODERATE_EDGE` and `NFL_STRONG_EDGE` (decimal fractions; defaults 0.02 and 0.05).

The market client caches event metadata for five minutes, books for 45 seconds, paces requests, and backs off after HTTP 429. If the book is rate limited, it shows the event's verified reference bid/ask with a **Reference quote · depth unavailable** label; the raw model-versus-market difference remains visible, while executable net edge is withheld. A book retained after a failed refresh is marked stale and also cannot produce a net-edge signal.

## API

| Endpoint | Description |
| --- | --- |
| `/api/meta` | Available seasons and weeks, current selection, data status |
| `/api/games?season=2026&week=11` | Complete NFL schedule for the week |
| `/api/predictions?season=2026&week=11` | Independent model output |
| `/api/total-predictions?season=2026&week=11` | Projected combined points and 80% interval |
| `/api/markets?season=2026&week=11&quantity=100` | Verified markets and quote depth |
| `/api/totals?season=2026&week=11&quantity=100` | Verified Polymarket full-game total lines |
| `/api/value?season=2026&week=11&quantity=100` | Model versus market valuation |
| `/api/dashboard?season=2026&week=11&quantity=100` | Combined board response |
| `/api/games/{game_id}/analysis` | Pregame metric comparison and provenance |
| `/api/model/metrics` | Walk-forward validation and calibration |
| `/api/model/totals-metrics` | Score regression walk-forward MAE, RMSE, bias, interval coverage |

API docs are available at `/docs`. A `refresh=true` query parameter bypasses the short market cache. Market quotes are requested only for games that have not kicked off.

## Verify

```bash
.venv/bin/python -m pytest -q
```

The integrity tests cover current-game leakage, strict winner and total market matching, short-side order book execution, and net-edge math.

## Known limits

This first model uses team-level quarterback history; it does not claim to know a confirmed future starting quarterback or injury status. Historical features start in 2015, so earlier schedule seasons have no model estimate. The uncertainty of an individual model probability is not yet quantified, and the app does not promote edge estimates as statistically dependable trading signals. Market quote coverage depends on the exchange listing and its API availability.
