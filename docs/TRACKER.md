# Tracker API: one read-only view of every paper bot

`backend/tracker` gathers every Beef Kirkey paper bot into one view. It reads each bot's journal and heartbeat and never writes to them. It is the data feed for **🎖️ Chief Beef Kirkey**, the separate tracker Discord bot in its own repository, `chief-beef-kirkey`.

| Bot | Source |
|---|---|
| 🏈 NFL Polymarket (`nfl`) | `data/trading` paper engine and worker heartbeat |
| ₿ BTC spot (`btc`) | `data/crypto` journal and `health.json` |
| 🎲 Crypto Up/Down (`updown`) | `data/updown` journal and `health.json` |
| 🥇 Gold Up/Down (`gold`) | `data/gold` journal and `health.json` |

Each bot gets a light:

- 🟢 `ok`
- 🟡 `warn`:
  - erroring
  - paused
  - at its daily loss limit
  - 20% or more below its starting balance
- 🔴 `down`:
  - no heartbeat, or a heartbeat older than 2–3 minutes
  - journal unreadable (this doesn't affect the other bots' entries)

## Endpoints (GET only)

| Endpoint | Returns |
|---|---|
| `/api/tracker/status` | Per bot: light, notes, heartbeat age, balance, all-time and today's P&L, open bets, $ at risk and recent record. Also totals across all bots. |
| `/api/tracker/trades?limit=30&desk=gold` | One normalized feed of trades, newest first. Each trade has `desk`, `id`, `market`, `side`, `stake`, `status` and `pnl`. `status` is `OPEN`, `WORKING`, `WIN`, `LOSS`, `OUT` (sold above fair value), `TARGET` (take-profit hit), `STOP` (stop-loss hit) or `FLAT`. |
| `/api/tracker/roster?desk=gold&limit=500` | Every trade per bot, with totals: record, outs, take-profits, stops, open bets, win rate, net P&L, amount staked and ROI. |
