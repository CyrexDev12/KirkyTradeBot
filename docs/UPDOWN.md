# Polymarket Up/Down 15-minute paper bot

**PAPER_ONLY.** The bot reads public market data and simulates fills against the live book. It never places, cancels or changes a real order. All Polymarket reads go through `backend.trading.venue.ReadOnlyVenueClient`. That client allows GET requests on listed routes only.

## What it trades

Polymarket US lists a crypto "Up or Down" market every 15 minutes for BTC, ETH, SOL and XRP. Each event slug carries the window start in UTC, for example `btc-updown-15m-2026-11-22-1915z`.

- The **price to beat** is the average of the 60 one-second CF Benchmarks index prices (BRTI for BTC) in the minute before the window starts.
- The **close** is the same kind of average for the minute before the window ends.
- **Up** pays $1 when close ≥ price to beat. A flat window counts as Up. **Down** pays $1 otherwise.
- The book is the YES (Up) book. Down is bought at 1 − best YES bid and sold at 1 − best YES ask.
- Taker fee per contract is `feeCoefficient × p × (1 − p)`, with feeCoefficient = 0.095. That is 2.4¢ at 50¢ and 0.9¢ at 90¢.

These details were checked against live responses on 2026-11-22. A trimmed capture is in `tests/fixtures/updown_capture_2026-11-22.json`.

## How it decides (every 5 s)

1. **Read prices.** It reads the window, its price to beat and the book. It also reads the settlement index: the last 120 one-second prices, and two hours of one-minute prices for volatility.
2. **Cross-check with Coinbase.** It reads the Coinbase BTC-USD, ETH-USD and SOL-USD tickers. If Coinbase and the index differ by more than 0.2%, it skips.
3. **Compute fair value.** It treats the price as a driftless random walk and accounts for the 60-second averaging at the close (`backend/updown/model.py`). Inside the last minute, it uses the part of the closing average already observed. On about 2.5 days of history it scored a Brier score of about 0.18 at minute 5. A coin flip scores 0.25.
4. **Pick a side.** Edge = fair value − ask − fee. It buys the side with the larger edge when that edge is at least 2¢ per contract.
5. **Filters.** The contract must be priced between 10¢ and 90¢; the model was overconfident at the extremes. Entries are allowed from 60 s after the window starts until 90 s before it ends, one bet per asset per window.
6. **Sell early or settle.** It sells early when a bid, after the exit fee, beats fair value by 1¢. Otherwise the bet settles on Polymarket's published settlement price. If that price hasn't appeared 30 minutes after the window ends, it settles on the index-derived close instead.

## Risk (stated on every entry card)

| Setting | Default | Env var |
|---|---|---|
| Stake per bet (= max loss) | ≤ 5% of balance | `UPDOWN_MAX_STAKE` |
| Sizing | ¼ Kelly on the after-fee edge | `UPDOWN_KELLY_FRACTION` |
| Total open stake | ≤ 15% of balance | `UPDOWN_MAX_OPEN_STAKE` |
| Daily stop | no new bets after −10% in an Arizona day | `UPDOWN_DAILY_LOSS_LIMIT` |
| Minimum edge | 2¢ per contract after fees | `UPDOWN_MIN_EDGE` |
| Price band | 10¢–90¢ | `UPDOWN_PRICE_RANGE` |
| Book participation | ≤ 50% of displayed size per level | — |
| Coinbase vs index gap | ≤ 0.2% | `UPDOWN_COINBASE_MAX_GAP` |
| Assets | btc,eth,sol | `UPDOWN_ASSETS` |

A bet can lose its whole stake. Fills are simulated, so real fills could be worse. The bot competes with faster traders who see the same index, so an edge the model sees may be stale information.

## Journal and outputs

- `data/updown/paper.sqlite3` has four tables:
  - `trades`
  - `windows`: price to beat, outcome and last skip reason
  - `observations`: one sample per minute of model fair value against the market, used for calibration
  - `events`: feeds Discord
- API: `/api/updown/account`, `/trades`, `/stats` and `/events`. `/stats` reports the model's Brier score against the market mid.
- Discord posts entries and closes to the crypto channel set with `/paper-channels`. Commands: `/updown-account`, `/updown-trades` and `/updown-stats`.
- Worker: `python -m backend.updown.worker`. Docker Compose service: `updown-worker`. Health file: `data/updown/health.json`.
