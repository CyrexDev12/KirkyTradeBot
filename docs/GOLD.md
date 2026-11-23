# Gold Up/Down paper desk

**PAPER_ONLY.** This desk runs the same engine as the crypto Up/Down bot ([UPDOWN.md](UPDOWN.md)) on Polymarket US's "Gold Up or Down" markets. It keeps its own $100 account in `data/gold/paper.sqlite3`.

## Markets

| Series | Window | Listed on 2026-11-22 |
|---|---|---|
| `gold-updown-5m-…` | 5 minutes | **not listed**. The bot checks once a minute and starts automatically if it appears. |
| `gold-updown-15m-…` | 15 minutes | yes |
| `gold-updown-1h-…` | hourly | yes |

- **Settlement.** Up if the gold price at the window end ≥ the price at the window start, else Down. Both prices are single Chainlink XAU/USD readings, rounded to the cent. They are not averages: the price to beat equals the 1-second index close at `windowStart`, verified on the 2026-11-20 18:00Z windows.
- **Hours.** Gold trades Sunday 7 PM to Friday 5 PM ET, with a daily break. Outside those hours the windows show `MARKET_STATUS_CLOSED` and the gold index is gap-filled, so the bot skips them.
- **Fees.** The taker fee is the same as crypto: `0.095 × p × (1 − p)` per contract.

## Pick → win / loss / out

1. **Pick.** The bot buys the side priced below its model value by at least 2¢ after fees. The model is `fair_up_point`: P(Up) = Φ(ln(S/O) / (σ√τ)). σ comes from two hours of one-minute gold index moves. It checks the index against **Coinbase PAXG-USD**, a token backed by gold, and skips if they differ by more than 1%. PAXG usually trades about 0.1–0.3% above spot.
2. **Out.** It sells early when the bid, after the exit fee, beats fair value by 1¢.
3. **Win / loss.** Otherwise the bet settles on Polymarket's published settlement price. If none appears within 30 minutes, it settles on the index price at the end second.

## Model check

On one-minute gold history from Nov 16–20, 2026, the Brier score was 0.16 halfway through both the 15-minute (455 windows) and hourly (110 windows) markets. A coin flip scores 0.25. Gold fell about 9% that week, so the mid-range estimates ran slightly high. `/gold-stats` keeps scoring the model against the market mid as live samples come in.

## Risk

Every pick card states the risk. The defaults match the crypto desk and are changed with `GOLD_*` environment variables:

| Limit | Default | Variable |
|---|---|---|
| Max stake per bet (the most you can lose) | 5% | `GOLD_MAX_STAKE` |
| Bet sizing | ¼ Kelly | `GOLD_KELLY_FRACTION` |
| Total open stake | 15% | `GOLD_MAX_OPEN_STAKE` |
| Daily stop | 10% | `GOLD_DAILY_LOSS_LIMIT` |
| Minimum edge after fees | 2¢ | `GOLD_MIN_EDGE` |
| Contract price range | 10–90¢ | `GOLD_PRICE_RANGE` |
| Max PAXG gap | 1% | `GOLD_COINBASE_MAX_GAP` |
| Horizons traded | 5m,15m,1h | `GOLD_HORIZONS` |

## Running it

- Worker: `python -m backend.gold.worker`, or the `gold-worker` Compose service.
- API: `/api/gold/{account,trades,stats,events}`.
- Discord:
  - Channel: `/paper-channels set gold #channel`. It gets pick, win, loss and out cards.
  - Commands: `/gold-picks`, `/gold-results`, `/gold-stats`, `/gold-account`.
