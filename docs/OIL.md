# Energy paper desks: 🛢️ WTI crude and 🔥 natural gas

The two desks run the same engine (`backend/wti`). Natural gas (`backend/natgas`) is covered at the end of this page; everything else here applies to both.

**PAPER_ONLY.** The desk paper-trades Polymarket US **"WTI Crude Up or Down"** markets on the same engine as the gold desk, with every gold feature. Five fair-value models compete live, each with its own $100 paper account, so the best one can be picked from real results.

## The markets (checked live 2026-11-22)

| Series | Window |
|---|---|
| `wti-updown-15m-…` | 15 minutes |
| `wti-updown-1h-…` | hourly |
| `wti-updown-1d-…` | daily: **6:15 PM ET → 5:00 PM ET** the next day. That's 23:15Z → 22:00Z in winter and 22:15Z → 21:00Z in summer. |

- **Settlement.** Up if the price at the end ≥ the price at the start. Both are single readings of the **Chainlink front-month WTI futures mid** (`NY-USOIL/USD-Streams-FuturesPrice`), rounded to the cent. The bot reads the same price from Polymarket as `commodity:wti`.
- **Fee.** The same as gold: `0.095 × p × (1 − p)` per contract.
- **Contract switch.** Chainlink picks which futures month it follows and when to move to the next one. It doesn't adjust the price at the switch, so a window that spans the switch compares two different contracts. The desk watches for this; see below.
- **Cross-check.** Coinbase has no oil market, so there's no outside price to compare against.

## Pick → win / loss / out, the same as gold

1. **Pick.** The bot buys the side priced below its model value by ≥ 2¢ after fees, in the 10–90¢ range.
2. **Sizing.** ¼-Kelly, ≤ 5% of balance per bet, ≤ 15% open at once, and a 10% daily stop.
3. **🎯 Take profit / 🛑 stop loss.** The ticket has a bracket: take profit at half the room to $1, stop at half the entry price. Each ticket states both prices and their P&L.
4. **💸 Out.** It sells early when the bid beats fair value.
5. **📰 Out before news.** New for oil: it sells just before a high-impact release if the bid at least gets the entry back (below).
6. **✅ Win / ❌ loss.** At close, any remaining position waits for the official Polymarket US contract payout.

The [shared execution engine](UPDOWN.md#how-it-decides-every-5-s) applies the same order latency, book freshness and depth rules to the oil and gas desks. Each model account tracks the depth it consumes independently.

## The five models

The [WTI 15-minute replay](WTI_REPLAY.md) compares fill rates, prices, and net P&L across these models under different delay and depth assumptions.

| Model | Idea | Why it might win |
|---|---|---|
| **A · Random walk** | Volatility from the last 2 hours, no drift. This is the gold bot's model. | Simple and well calibrated on calm days |
| **B · Fast volatility** | Volatility weighted to the last ~10 minutes | Reacts when oil gets busy |
| **C · Trend follower** | Leans with the trend when the 5- and 20-minute averages agree with the last 15 minutes | Rides real moves |
| **D · Market blend** | Halfway between A and the order book's own price | Trusts the market when it knows something |
| **E · News-aware** ⭐ | B, plus the extra move each scheduled release inside the window can add, and higher volatility for a while after a price shock | Built for oil's news calendar |

Checked on WTI 1-minute history from Nov 18–20, 2026 (227 fifteen-minute and 55 hourly windows):

- A and B scored a Brier score of **0.15** halfway through a 15-minute window and **0.14** for hourly windows. A coin flip scores 0.25.
- A plain momentum drift scored worse early in the window, so C only trends when two averages agree, and uses a gentler drift.

`/oil leaderboard` ranks the five live accounts. The **lead model** (`WTI_LEAD_MODEL`, default `E`) is only the one whose cards post to the oil channel. All five trade on paper either way.

## Information coming in and out

**Scheduled releases** follow New York time. Add, change or skip them in `data/wti/events.json`.

| Release | When | Impact |
|---|---|---|
| EIA Weekly Petroleum Status Report | Wed 10:30 AM | high |
| US jobs report | first Fri 8:30 AM | high |
| API weekly crude inventory | Tue 4:30 PM | medium |
| Baker Hughes rig count | Fri 1:00 PM | low |
| CME crude settlement window | weekdays 2:28 PM | low |

What the desk does with them, for all models:

- **No new entries** from 2 minutes before to 1 minute after a high-impact release (`WTI_NEWS_HOLD_BEFORE` / `_AFTER`).
- **Get out first.** Within 3 minutes of a high-impact release that falls inside an open bet's window, the bet sells if the bid at least gets the entry back (`WTI_PRE_NEWS_EXIT`). It never sells at a loss just because news is coming.
- **Model E** adds the move a release typically causes to its odds. The priors are 0.35% of price for high impact, 0.20% for medium and 0.07% for low. The live journal shows whether they need tuning, and `events.json` can set `jump` per event.
- **Alerts.** The oil channel posts high and medium releases 30 minutes ahead.

**Detected from the price**, every 5 seconds:

- **⚡ Shock.** A 10-second move of 6 or more typical 10-second moves. That's news hitting the market. New entries pause for 60 seconds, and model E raises its volatility, decaying over about 10 minutes.
- **Possible contract switch.** A one-minute jump of 0.8% or more with no scheduled release nearby. New entries pause for 30 minutes. Known switch days can also be listed in `roll_dates`.
- **Front month.** `/oil info` shows the front futures month and its last trading day. That's three business days before the 25th of the prior month; exchange holidays aren't modelled.

There is **no live headline feed**. Unscheduled news (OPEC comments, geopolitics) reaches the desk only through the price. Add known one-offs, such as OPEC+ meetings, CPI or FOMC, to `events.json`:

```json
{"events": [{"at": "2026-11-30T09:00:00", "name": "OPEC+ meeting", "impact": "high", "jump": 0.01}],
 "roll_dates": ["2026-12-18"],
 "skip": ["2026-11-26 EIA Weekly Petroleum Status Report"]}
```

Times without a timezone are New York time.

## Discord

- **Channel.** `/paper-channels set oil #channel` gets the lead model's 🛢️ PICK, ✅ WIN, ❌ LOSS, 💸 OUT, 🎯 TAKE PROFIT, 🛑 STOPPED and 📰 OUT BEFORE NEWS cards, plus 📰 coming-up and ⚡ shock alerts.
- **Commands.** These take an optional model; the default is the lead model.

| Command | Shows |
|---|---|
| `/oil leaderboard` | All five models side by side: balance, record, ROI, open bets, accuracy against the market |
| `/oil picks [model]` | Open picks with their take-profit and stop prices |
| `/oil results [model]` | Recent wins, losses and outs |
| `/oil account [model]` | Balance, open bets and risk limits |
| `/oil stats [model]` | Record and accuracy against the market |
| `/oil info` | Releases ahead, shocks and switches detected, the front futures month |

- **Chief Beef Kirkey** tracks each oil model as its own bot (`oil-A` … `oil-E`) in `/chiefkirkey status` and `fullroster`.

## Running it

- **Worker:** `python -m backend.wti.worker`, or the `wti-worker` Compose service. It writes `data/wti/health.json`.
- **Journals:** `data/wti/models/<A–E>/paper.sqlite3`, plus `data/wti/info.sqlite3` for detected and announced news.
- **API:**
  - `/api/wti/models` (the leaderboard)
  - `/api/wti/info` and `/api/wti/info/events`
  - `/api/wti/{model}/account`, `/trades`, `/stats` and `/events`
- **Settings:** `WTI_HORIZONS`, `WTI_MODELS`, `WTI_LEAD_MODEL`, the news timings above, and the shared `WTI_MIN_EDGE`, `WTI_MAX_STAKE`, `WTI_TAKE_PROFIT`, `WTI_STOP_LOSS`, …

## 🔥 Natural gas

The gas desk is the same engine with gas settings (`NATGAS_*`). Its journals live in `data/natgas`, and it runs as the `natgas-worker` service.

| | Natural gas |
|---|---|
| Markets | `natgas-updown-15m / 1h / 1d-…`. The daily window is the same as WTI: 6:15 PM → 5 PM ET. |
| Settles on | The Chainlink **front-month Henry Hub futures mid** (`NatGas/USD-Streams-FuturesPrice`), rounded to **4 decimals** |
| Price feed | `commodity:natgas` |
| Front month | Last trades three business days before the first day of the delivery month. For example, Dec 2026 last trades Nov 26 and Jan 2027 last trades Dec 29. Exchange holidays aren't modelled. |
| Contract-switch jump | 1.5% in a minute with no news. Gas regularly moves about 0.7% in a minute, and crude's threshold is 0.8%. |

**Gas news calendar** (New York time; edit `data/natgas/events.json`):

| Release | When | Impact | Move prior |
|---|---|---|---|
| EIA Weekly Natural Gas Storage Report | Thu 10:30 AM | high | 0.60% |
| NOAA 6–10 / 8–14 day weather outlooks | weekdays 3:00 PM | medium | 0.25% |
| Baker Hughes rig count | Fri 1:00 PM | low | 0.10% |
| CME natural gas settlement window | weekdays 2:28 PM | low | 0.10% |

**What the history says.** On Nov 18–20, 2026, typical 1-minute moves rose from 11 to 18 basis points in the 10 minutes after the storage report. That's why gas uses bigger move priors than crude. Models A and B scored a Brier score of 0.17 halfway through 15-minute windows and 0.15 for hourly. A coin flip scores 0.25.

**Holiday weeks.** The storage report moves on holiday weeks. Over Thanksgiving it's usually Wednesday. Add the real time to `events.json` and skip the Thursday one:

```json
{"events": [{"at": "2026-11-25T12:00:00", "name": "EIA Weekly Natural Gas Storage Report", "impact": "high"}],
 "skip": ["2026-11-26 EIA Weekly Natural Gas Storage Report"]}
```

**Discord:**

- **Channel.** `/paper-channels set gas` gets the lead model's 🔥 cards, plus storage-report, weather-outlook and ⚡ shock alerts.
- **Commands.** `/gas leaderboard`, `/gas picks`, `/gas results`, `/gas account`, `/gas stats` and `/gas info`.
- **Chief Beef Kirkey** tracks `gas-A` … `gas-E`.

**API.** `/api/natgas/models`, `/api/natgas/info` and `/api/natgas/{model}/…`, the same routes as `/api/wti`.
