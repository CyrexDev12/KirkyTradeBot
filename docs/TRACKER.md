# Tracker API: one read-only view of every paper bot

`backend/tracker` gathers every Beef Kirkey paper bot into one view. It reads each bot's journal and heartbeat and never writes to them. It feeds **🎖️ Chief Beef Kirkey**, whose commands are part of the Beef Kirkey Discord bot (`bot/tracker.py`).

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

## In Discord (Beef Kirkey)

| Command | Shows |
|---|---|
| `/chiefkirkey status` | Every bot: light, balance, all-time and today's P&L, open bets and $ at risk, record, heartbeat |
| `/chiefkirkey trades [bot]` | The latest trades across all bots, or one bot's |
| `/chiefkirkey bot <bot>` | One bot in depth: status, notes and its recent trades |
| `/chiefkirkey fullroster [bot] [show]` | **Every trade by every bot.** A totals card, then each bot's trades as aligned tables (result, market, side, stake, P&L, Arizona time), 40 rows per page. `show` picks all, open or closed trades. |
| `/chiefkirkey alerts` | Only the bots that are 🟡 or 🔴, and why |

`/paper-channels set chief #channel` turns on the channel feed:

- **📡 Trade feed.** Every new open (🎟️) and every result: ✅ win, ❌ loss, 💸 out, 🎯 take-profit or 🛑 stopped.
- **🚨 Alerts.** Posted when a bot turns 🟡 or 🔴, and again when it recovers.
- **Status reports.** Posted at 8 AM and 8 PM Arizona (`CHIEF_REPORT_HOURS`).

A new channel starts from the current state, so it won't flood old trades. The same commands are also packaged as a standalone bot in the separate `chief-beef-kirkey` repository. If you run both, set a feed channel in only one of them, or every post will appear twice.
