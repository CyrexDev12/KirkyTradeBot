# NFL Fair Value

Local, read-only NFL pregame analytics dashboard. It estimates each team's full-game win probability and the combined final score from NFLverse history, then compares those estimates with live [Polymarket US](https://docs.polymarket.us/data-guide/overview) moneyline and game-total contracts. It does not place trades.

## Run

The Discord companion provides pregame alerts, matchup buttons, weekly NFL parlays, and separate NFL and BTC paper accounts. Use `/parlay` for the informational weekly card, `/account` for NFL and `/crypto-account` for BTC. A server manager sets publication channels with `/paper-channels set`. The live Azure VM is in Mexico Central. See the [existing VM update guide](deploy/UPDATE_AZURE.md) or the [new installation guide](deploy/README.md).

Run `/paper-channels setup` as a server manager and choose a BTC channel and a weekly parlay channel. Use `/paper-channels show` to check the destinations. The bot saves these choices across restarts.

The weekly NFL board mixes two shorter two-leg tickets with a three-, four- and five-leg ticket when the slate qualifies. Each ticket uses different games within it. A moneyline or over/under leg needs at least 55% support from both the gradient and Monte Carlo models. The board shows both forecasts alongside each leg’s American and decimal odds. It lists each ticket’s model hit chance, rating and risk score. The hit estimate multiplies the weaker forecast on each leg, assumes independence between games and treats moneyline results as conditional on a decisive game. The rating follows the weakest leg. Risk rises as the chance of landing every pick falls.

The payout multiplier is the product of the exact selections’ NFLverse reference decimal odds. For example, two picks at −200 each yield 1.50 × 1.50 = 2.25×, or +125 combined. A winning $10 example returns $22.50 including the stake. The break-even percentage is one divided by the multiplier. The schedule has no sportsbook promotional-boost terms or guaranteed parlay quote. Missing prices are shown individually and the combined payout is available when every leg is priced. Parlays are informational and do not create positions. [NFLverse schedule fields](https://nflreadr.nflverse.com/articles/dictionary_schedules.html)

The BTC worker evaluates every 15-minute window (:00, :15, :30, :45) around the clock by default. `CRYPTO_TRADING_HOURS=6-9` and `CRYPTO_WINDOW_MINUTES=30` restore the original six morning slots in `America/Phoenix`. It uses eight hours of completed five-minute Coinbase BTC-USD candles to form 15-minute trend and volatility estimates. Each slot can produce a paper long, a paper short, or a skip with a reason. The BTC channel receives entries, exits and skips after setup. Use `/crypto-trades` for recent history. [Coinbase candle format](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)

The BTC account starts at $100. Position sizing reduces the amount as trend quality declines. At full strength, the position is capped at 50% of balance and the planned loss at the stop at 1.5% (`CRYPTO_MAX_POSITION`, `CRYPTO_RISK_PER_TRADE`). The target is "any profit": the nearest price that still nets 0.05% of the position after both fees and slippage (`CRYPTO_MIN_PROFIT`). A 5% daily realized loss pauses further entries that Arizona day (`CRYPTO_DAILY_LOSS_LIMIT`). Every entry card states the dollar loss at the stop, the position size and the daily limit. The simulation crosses the observed spread and assumes a 0.4% fee (`CRYPTO_FEE_RATE`; US Coinbase Advanced lists 0.50% maker / 0.90% taker at the entry tier) and 0.02% slippage per side. A replay of Nov 5–22, 2026 with these settings lost money (fees exceed the trend signal's edge), so treat it as an experiment. It skips signals whose target is more than three recent average ranges away. Open positions close on an observed stop or target, or on the first usable quote at or after the 15-minute window ends. Prices can move through a stop between observations. These are paper strategy assumptions with no established profitability record.

The separate **Polymarket Up/Down** paper bot (`backend/updown`, see [docs/UPDOWN.md](docs/UPDOWN.md)) prices BTC, ETH and SOL 15-minute Up/Down contracts from the settlement index with Coinbase as a cross-check, and posts its entries and settlements to the same crypto channel. Use `/updown-account`, `/updown-trades` and `/updown-stats`.

The **gold** paper desk ([docs/GOLD.md](docs/GOLD.md)) runs the same engine on Polymarket's Gold Up or Down 15-minute and hourly markets (5-minute too once listed), with Coinbase PAXG-USD as the cross-check. Pick a channel with `/paper-channels set gold`; it posts every pick and its win, loss or early out. Use `/gold-picks`, `/gold-results`, `/gold-stats` and `/gold-account`.

**🎖️ Chief Beef Kirkey** ([docs/TRACKER.md](docs/TRACKER.md)) tracks every paper bot: NFL, BTC spot, crypto Up/Down and gold.
- **Commands:** `/chiefkirkey status`, `/chiefkirkey trades`, `/chiefkirkey bot`, `/chiefkirkey fullroster` (every trade by every bot, as clean tables) and `/chiefkirkey alerts`.
- **Channel feed** (`/paper-channels set chief`): new trades and results, alerts when a bot goes down or hits a loss limit, and 8 AM / 8 PM Arizona status reports.
- **Data:** it reads the read-only `/api/tracker/status`, `/api/tracker/trades` and `/api/tracker/roster` endpoints.

`/crypto-stats` shows lifetime wins, losses, break-evens, and profit after costs. Win rate divides profitable trades by all closed trades. Skips and open positions are counted separately. To adjust its next position, the worker looks at up to 40 recent trades in the same direction and Arizona hour over 90 days. With at least 20 usable outcomes, under 40% wins, and losses on average per dollar at risk in both halves of the sample, it halves the usual size. It can return to its usual size as evidence changes. Delayed exits more than a minute past the window count toward lifetime results only.

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
- Rolling score and efficiency features limit the influence of unusual values using the median and median absolute deviation of earlier games. The totals model uses absolute error during training. Final scores and realized errors are retained for evaluation and totals interval calibration.
- The win-probability model is a deterministic histogram gradient-boosted classifier. It has no Polymarket price input. For each walk-forward season, a logistic calibration mapping is fit only on earlier out-of-sample seasons; the production mapping is fit on past seasons. Home and away probabilities sum to one. Ties are rare and excluded from binary model training; market tie settlement is shown in game details.
- A separate histogram gradient-boosted regression model predicts combined final points, including overtime. It uses only pregame rolling team points scored and allowed, offensive and defensive efficiency, recent scoring, play volume, rest, week, and season. Its over/under probability at each listed line comes from the empirical distribution of prior-season out-of-sample score errors. The 80% interval is built from those errors. Exchange prices never enter this model.
- Validation is season walk-forward: each test season is predicted by a model trained on earlier seasons. Historical predictions are labeled **reconstructions**, because they were not contemporaneously archived. The validation endpoint reports Brier score, log loss, accuracy, season breakdowns, and calibration bins. Trading performance is intentionally absent without real timestamped historical quotes.
- Predictions generated before kickoff are cached and frozen after kickoff. Market prices are never used to retrain the model. If data or model artifacts are unavailable, the UI shows an error or empty value instead of a substitute estimate.

The current walk-forward scores and interval coverage are available at `/api/model/metrics` and `/api/model/totals-metrics` after the models finish training.

## Market matching and valuation

The app queries the public [NFL league events endpoint](https://docs.polymarket.us/api-reference/sports/get-events-by-league-slug) and verifies exact home and away team identifiers and roles, kickoff time, the `football_team_full_game_winner` market type, moneyline status, and settlement language. It then retrieves each matched [event detail](https://docs.polymarket.us/api-reference/events/get-event-by-id) and selects only active `football_team_full_game_total` combined-score, full-game, half-point lines with explicit Over and Under sides. Team totals, period totals, spreads, and props are excluded. Unverified games stay on the schedule with **Market Unavailable**.

The board features the total line whose exchange display price is closest to 50%, and matchup details list every verified full-game total line. Over is the long contract and Under is the short contract. The featured line uses a live order book for quantity-aware net edge; the rest of the ladder uses current event reference quotes and shows model-versus-market differences without an executable edge. If the exchange has no total contract for a game, the projected score still appears.

Polymarket US represents a two-team winner market with one long/short instrument. To buy the long team's outcome, the executable starting price is the best ask. To buy the other team's outcome, it is `1 − best bid`. The dashboard uses the full [order book](https://docs.polymarket.us/api-reference/markets/get-market-book) to compute a quantity-weighted purchase price for the selected trade size. A signal is suppressed when the depth is insufficient, the market is closed, the fee coefficient is missing, or a retained quote is stale.

Net edge is model fair value minus book execution cost minus estimated taker fees. Fee estimates use each consumed book level and the market's current coefficient under the [Polymarket US fee formula](https://docs.polymarket.us/fees): `coefficient × contracts × price × (1 − price)`. Actual fees can differ if orders split across resting fills or the user's fee tier differs. The dashboard displays estimated value rather than a recommendation or a demonstrated profitable strategy. Thresholds can be set with `NFL_MODERATE_EDGE` and `NFL_STRONG_EDGE` (decimal fractions; defaults 0.02 and 0.05).

The market client caches event metadata for five minutes, books for 45 seconds, paces requests, and backs off after HTTP 429. If the book is rate limited, it shows the event's verified reference bid/ask with a **Reference quote · depth unavailable** label; the raw model-versus-market difference remains visible, while executable net edge is withheld. A book retained after a failed refresh is marked stale and also cannot produce a net-edge signal.

## Current-season Monte Carlo

Select **Current-season Monte Carlo** or **Compare models** in the board's model selector. The simulator uses completed regular-season scoreboard results from the selected NFL season. Each of 50,000 seeded draws produces paired home and away final scores including overtime. Cards show expected points, unconditional win probabilities, final ties, and an 80% predictive interval for combined points. Details include total and margin histograms, verified exchange total lines, team evidence, and experimental settlement-aware net values. Compare mode shows home win probabilities conditional on no final tie for comparison with the existing binary model.

The scoring fit uses these equations.

```text
home mean = b + home scoring strength - away score prevention + h/2
away mean = b + away scoring strength - home score prevention - h/2
```

The intercept is unpenalized. Team coefficients have ridge penalty 4 and the home-margin coefficient has penalty 16. A game contributes two score rows with recency weight `2 ** (-age_days / 28)`. The score fit uses Huber weighting so large fitted errors have less influence. The free intercept and equal positive penalties force each family of team effects to average zero. Neutral games use zero venue adjustment. Every fitted coefficient, baseline, and residual comes from the selected season. Predictions are independent of prices, quantities, and the earlier-season models.

Public availability requires four games for each team, 40 forward error pairs, and residual effective sample size 25. Four or five team games receive a **Limited sample** label. The weighted points per game baseline bounds extreme scored and allowed values once a team has six games. The matchup detail shows the original averages and the number of adjustments. A previous-week fit predicts both sides of each earlier game. The simulator bounds the influence of extreme errors and samples the centered errors in their original pairs. Neutral-site banks additionally include equally weighted swapped pairs. The rounding rule is `max(0, floor(mean + error + 0.5))`. All scores, margins, totals, and probabilities use those same draws.

### Cutoffs, storage and reproducibility

The schedule source provides scores and kickoff times without historical completion or ingestion timestamps. This implementation conservatively admits a result on the next Tuesday at noon Eastern and requires its week to precede the target week. Thus live forecasts also exclude same-week results. Explicit availability timestamps, when supplied, must precede the cutoff. Invalid, incomplete, conflicting, unsupported, and unknown-venue records are excluded. NFL season comes from the schedule's season field, including January games. The schedule schema and Eastern kickoff convention are documented by NFLverse. [Schedule dictionary](https://nflreadr.nflverse.com/articles/dictionary_schedules.html)

The separate `data/monte_carlo.sqlite3` database records the first MC observation of each admitted result revision, complete eligible input snapshots, reconstructed error banks, configuration, seeded distributions, and every pregame artifact. SQLite transactions serialize concurrent builds and atomically commit results. Forecasts retain their fixed cutoff until eligible inputs, target scheduling information, or model configuration changes. Quote polls and quantity changes reuse stored forecasts. Paired score counts and discrete marginal histograms retain the sampled distribution.

The latest actual pregame artifact freezes when kickoff passes or the game is observed to have started. The frozen marker survives a later kickoff change. Missing pregame records show **No archived pregame MC prediction**. Historical evaluation runs separately as a reconstruction. Current-source corrections can alter a new reconstruction, while stored pregame snapshots retain their original inputs.

MC preparation runs separately from historical model training at startup and every 30 minutes. Loading the MC board can also prepare missing upcoming artifacts. It shares the six-hour NFLverse schedule cache. MC works using schedules alone.

Configuration comes from these environment variables.

| Variable | Default |
| --- | --- |
| `NFL_MC_SIMULATIONS` | 50000 |
| `NFL_MC_HALF_LIFE_DAYS` | 28 |
| `NFL_MC_TEAM_RIDGE` | 4 |
| `NFL_MC_HOME_RIDGE` | 16 |
| `NFL_MC_MIN_TEAM_GAMES` | 4 |
| `NFL_MC_RESIDUAL_WARMUP_TEAM_GAMES` | 2 |
| `NFL_MC_MIN_RESIDUAL_PAIRS` | 40 |
| `NFL_MC_MIN_RESIDUAL_EFFECTIVE_N` | 25 |
| `NFL_MC_BASE_SEED` | 17 |

These are provisional engineering settings. MC signal badges are disabled in this release. Net values use the existing depth and fee calculation after mapping verified contract payouts, including explicitly recognized $0.50 final-tie settlement. Unrecognized settlement terms return no fair value or executable edge. Displayed values assume normal full-game settlement, and the exchange's rescheduling terms still apply. Changing the total line queries the cached histogram.

### Prepare, inspect and evaluate

On Windows, create the environment with `python -m venv .venv` and install dependencies with `.venv\Scripts\python -m pip install -r requirements.txt pytest`. Use `.venv/bin/python` on macOS/Linux.

```powershell
.venv\Scripts\python -m backend.mc_validation --season 2026
.venv\Scripts\python -m backend.mc_validation --season 2026 --game 2026_11_IND_HOU
.venv\Scripts\python -m backend.mc_validation --season 2026 --evaluate --as-of 2026-11-17T17:00:00+00:00
.venv\Scripts\python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The game command prints the complete archived forecast, every input result, game weights, and all reconstructed residual pairs. The evaluation command prints binary and three-way scores, calibration bins with counts, score errors, signed bias, interval coverage and width, tails, ties, and clipping diagnostics. Totals probability calibration uses the predefined lines 37.5, 44.5, and 51.5. Numerical log-loss clipping is fixed at `1e-6`. Run the existing `backend.prepare` command first to include its held-out forecasts in comparisons.

Legacy comparisons accept season-held-out artifacts on matching games when the legacy feature history contains the same eligible season prefix. Additional earlier games in the target week are accepted only when neither target team participates, since those rows cannot update either team's stored features. Live production predictions are excluded from this historical comparison. The weighted-PPG baseline uses the same games and cutoff as MC.

### Initial 2026 reconstruction

With model `paired-scores-v1.0`, default settings, and cutoff November 17, 2026 at 10 AM Arizona time, 71 of 150 admitted completed games were eligible for public MC forecasts, giving 47.3% coverage. All 71 also had comparable existing-model reconstructions.

| Matched-game metric | Monte Carlo | Existing model | Weighted PPG |
| --- | --- | --- | --- |
| Binary Brier score, excluding actual ties | 0.2155 | 0.2339 | — |
| Binary log loss | 0.6195 | 0.6592 | — |
| Binary accuracy | 66.2% | 62.0% | — |
| Total MAE, points | 10.66 | 10.67 | 10.78 |
| Total RMSE, points | 13.88 | 13.67 | 13.96 |
| Total signed bias, projected minus actual | +0.57 | −0.24 | +0.55 |

The MC 80% total interval covered 78.9% of outcomes, with average width 33.63 points. Mean predicted tie probability was 3.91% and observed final ties were zero. This is an initial reconstructed sample. Forward archives are required to assess continued performance and tie calibration.

For the archived Colts-at-Texans artifact created November 17 at 10:07 AM Arizona time, nine Houston games, ten Indianapolis games, and 118 paired errors produced expected scores of Houston 26.9 and Indianapolis 22.3. The total mean was 49.2 with an 80% interval of 34–66. Home win, away win, and final tie probabilities were 64.22%, 32.87%, and 2.91%. The CLI exposes its complete source snapshot. Future eligible results or revised settings produce a new upcoming artifact.

The pooled residual bank has finite support and cannot capture every matchup's volatility, lineup changes, or injuries. More draws reduce sampling noise. At a 50% estimate and 50,000 draws, numerical standard error is about 0.224 percentage points. Team-rating and distribution-model uncertainty require separate forward validation. Final-tie frequency is especially sensitive to integer rounding. Predictions remain labeled **Experimental score approximation**.

## Paper trading (PAPER_ONLY)

A separate `paper-worker` service runs a persistent $100 **paper** account. It records public Polymarket US books, plans weekly contract-price trades only when both the GB model and the MC model support the same contract after costs, simulates fills conservatively against observed depth, and posts plans, trade cards and weekly reports to Discord (`/picks`, `/trade`, `/account`, `/performance`, `/why-no-trades`, `/health`). It never submits real orders. The current strategy can use labeled modeled-path estimates before recorded market-path history is sufficient; missing model inputs or failed screening gates produce no qualifying trades. See [docs/PAPER_TRADING.md](docs/PAPER_TRADING.md) for setup, rules and the capability report. The `/api/paper/*` endpoints are listed at `/docs`.

## Discord player yardage and graphs

- `/player-stats player:<name> stat:rushing|passing` estimates QB passing or rushing yards and RB rushing yards. It defaults to the player's next confirmed game. Optional `team`, `season`, and `week` identify a specific matchup. An earlier week returns a labeled pregame reconstruction. The response includes the season average, opponent yards allowed, projected workload, and an 80% prediction interval.
- `/paper-channels set kind:players channel:#player-stats` sets the weekly player props channel. The board adds up to ten qualifying picks from verified Polymarket US lines for games still to be played. The number of picks depends on available lines and prices. The bot updates one board per server and NFL week.
- `/pygraph dataset:team-points subject:KC` attaches a PNG and CSV of points scored and allowed, including the robust values used to inspect outliers. Use `dataset:rushing` or `dataset:passing` with a player's name in `subject` to inspect yardage history and the model estimate. `season` is optional, and player charts also accept `week` and `team`.

The yardage model blends recent and longer player history, carries or attempts, and efficiency. It adjusts for the opponent's yards allowed relative to the league. Robust limits reduce the influence of unusual yardage, workload, and defensive games. The interval uses the middle 80% of scaled errors from a chronological replay of the previous season. Picks compare model probabilities conditional on participation with the estimated cost of ten contracts, including exchange fees. A player with no qualifying snap settles under the venue's last fair market price rule.

## Exporting the live data

`bash deploy/azure/export.sh SUBSCRIPTION_ID [--only gold,bot] [--csv]` pulls a consistent snapshot of every bot's data off the Azure VM without stopping anything. See [deploy/EXPORT_AZURE.md](deploy/EXPORT_AZURE.md).

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
| `/api/monte-carlo/predictions?season=2026&week=11` | Current-season forecasts and structured availability states |
| `/api/games/{game_id}/monte-carlo` | One archived MC forecast with distributions and provenance |
| `/api/model/monte-carlo-metrics?season=2026` | Chronological reconstruction, coverage and matched-game metrics |
| `/api/players/projection` | Pregame player yardage estimate and supporting game history |
| `/api/players/picks` | Ranked weekly rushing and passing props with verified quotes |
| `/api/graphs/team-points?team=KC` | Original and adjusted regular-season team scoring data |

The dashboard accepts `model=existing`, `model=monte_carlo`, or `model=compare`. Its default is `existing`.

API docs are available at `/docs`. A `refresh=true` query parameter bypasses the short market cache. Market quotes are requested only for games that have not kicked off.

## Verify

```bash
.venv/bin/python -m pytest -q
```

The integrity tests cover current-game leakage, strict winner and total market matching, short-side order book execution, and net-edge math.

Paper-trading tests (`tests/test_paper_trading.py`) cover fills, liquidity reuse, partial exits, stop gaps, outages, settlement, sizing, gates, rating thresholds, censoring, restart, deterministic replay, Discord reconciliation, and the PAPER_ONLY guard.

MC tests additionally cover season and cutoff isolation, incomplete results, weighted ridge effects, forward residual provenance, neutral symmetry, paired score coherence, deterministic concurrent caches, kickoff freezing, schedule changes, rounding, numerical convergence, settlement mapping, matched evaluation, and API mode compatibility.

## Known limits

This first model uses team-level quarterback history; it does not claim to know a confirmed future starting quarterback or injury status. Historical features start in 2015, so earlier schedule seasons have no model estimate. The uncertainty of an individual model probability is not yet quantified, and the app does not promote edge estimates as statistically dependable trading signals. Market quote coverage depends on the exchange listing and its API availability.
