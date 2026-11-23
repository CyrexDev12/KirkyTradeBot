# 🎖️ Chief Beef Kirkey — the bot that tracks the bots

Chief Beef Kirkey watches every Beef Kirkey paper bot. It reads each bot's own journal and health file without changing them, so it can't affect their trades.

| Bot | What Chief reads |
|---|---|
| 🏈 NFL Polymarket | `data/trading` paper engine and worker heartbeat |
| ₿ BTC spot (Coinbase) | `data/crypto` journal and `health.json` |
| 🎲 Crypto Up/Down 15m | `data/updown` journal and `health.json` |
| 🥇 Gold Up/Down | `data/gold` journal and `health.json` |

Each bot gets a light:

- 🟢 **healthy**
- 🟡 **needs attention**:
  - erroring
  - paused
  - at its daily loss limit
  - 20% or more below its starting balance
- 🔴 **down**:
  - no heartbeat
  - heartbeat older than 2–3 minutes
  - journal unreadable

## In Discord

Set its channel with `/paper-channels set chief #channel`. It posts three kinds of card:

- **📡 Trade tracker.** Every new open and every ✅ win, ❌ loss or 💸 out across all bots, batched into one card per poll. A new channel starts from the current state, so it won't flood you with old trades.
- **Alerts.** Posted when a bot turns 🔴 or 🟡, and again when it's back to 🟢.
- **Status reports.** Posted at 8 AM and 8 PM Arizona time. Change the hours with `CHIEF_REPORT_HOURS`, for example `6,12,18`.

## Commands

| Command | Shows |
|---|---|
| `/chiefkirkey status` | Every bot: light, balance, all-time and today's P&L, open bets and $ at risk, recent record, heartbeat |
| `/chiefkirkey trades [bot]` | The latest trades across all bots, or one bot's |
| `/chiefkirkey bot <bot>` | One bot in depth: status, notes and its recent trades |
| `/chiefkirkey fullroster [bot] [show]` | **Every trade by every bot**, grouped by bot, as aligned tables with the result, market, side, stake, P&L and Arizona time. Starts with totals for all bots and each bot: record, win rate, net P&L, staked and ROI. `show` picks all, open or closed trades. Long histories are split into pages of 40. |
| `/chiefkirkey alerts` | Only the bots that are 🟡 or 🔴, and why |

## API

The same data is available read-only:

- `GET /api/tracker/status`
- `GET /api/tracker/trades?limit=30&desk=gold`
- `GET /api/tracker/roster?desk=gold&limit=500`: every trade per bot, with totals
