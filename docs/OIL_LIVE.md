# Oil D live pilot

The live worker trades only Oil D (Market blend) on 15-minute WTI Up/Down contracts and uses its own order journal. The selected mode determines whether entries follow paper intents or the WebSocket signal evaluator. Entries are automatic with configured credentials and the operator pause cleared. The paper models keep generating signals and results in parallel.

## Before enabling entries

Confirm that Polymarket US has given you written approval for live API access and that you can run that access from the VM's hosting location. The current Azure VM is in Mexico Central. Apply any limits that Polymarket US places on your access. [Individual Participant and Clearing Member Agreement](https://www.polymarketexchange.com/files/legal/latest/participant-agreement)

Use an approved Polymarket US account with the $10–$12 you intend to allocate. The worker skips entries while the exchange account has another position or open order. It allows at most one new entry attempt per 15-minute window and one active trade at a time. The proposed cost including the fee reserve is capped at $1 per entry. The pilot sizes new entries against a $2 net loss limit and pauses if it reaches that limit. Execution and fee estimates can differ from the exchange's actual result. [Polymarket US authentication](https://docs.polymarket.us/api-reference/authentication)

## Install the update

Use `.deploy/nfl-oil-d-paper-limits-azure.zip` and follow the [Azure update steps](../deploy/UPDATE_AZURE.md). The updater starts the new worker after the existing services pass their checks, including the event feed used by the Discord publisher. Set up the Discord destination before adding credentials.

In the configured Discord server, an administrator can use these commands.

| Command | What it does |
| --- | --- |
| `/oil-live channel` | Choose the channel for live order and status updates. |
| `/oil-live status` | Check new-entry state, worker status, pilot limits, open exposure, and paper comparisons. |
| `/oil-live chart time:24h` | Plot realized P&L and the starting budget plus that P&L over the selected period. |
| `/oil-live off` | Persistently pause new entries. The worker continues managing confirmed exposure and settlement. |

## Configure the approved credentials

Connect to the Azure VM by SSH using the [reconnect steps](../deploy/UPDATE_AZURE.md#reconnect-inspect-logs-or-recover). Run this block on the VM. Both prompts hide the input. The environment file stays on the VM with private permissions.

```bash
cd ~/nfl-fair-value
(
  set -euo pipefail
  umask 077
  read -rsp "Polymarket US live key ID: " wti_live_id
  printf '\n'
  read -rsp "Polymarket US live secret key: " wti_live_secret
  printf '\n'
  [[ -n "$wti_live_id" && -n "$wti_live_secret" ]]

  mkdir -p .secrets
  wti_live_env="$(mktemp .secrets/wti-live.env.XXXXXX)"
  trap 'rm -f -- "$wti_live_env"' EXIT
  printf 'WTI_LIVE_KEY_ID=%s\nWTI_LIVE_SECRET_KEY=%s\nWTI_LIVE_BUDGET_USD=12\nWTI_LIVE_ENABLED=true\n' \
    "$wti_live_id" "$wti_live_secret" > "$wti_live_env"
  mv -f -- "$wti_live_env" .secrets/wti-live.env
  unset wti_live_id wti_live_secret
  sudo docker compose up -d --no-deps --force-recreate wti-live-worker
)
```

`WTI_LIVE_BUDGET_USD` defaults to 12. Use 10 if you funded $10. `WTI_LIVE_ENABLED` defaults to true. A durable pause survives environment changes and container restarts. The Discord group provides an off command.

An explicitly started pilot records its allocation and starting P&L in the live journal. The next $10 pilot has its own $2 realized loss limit, and the lifetime ledger retains all earlier trades. Its budget remains capped by the existing environment setting. The live chart shows the starting allocation plus realized P&L. The worker checks actual buying power separately before submitting orders.

Check `/oil-live status` after starting the worker. Compare live and paper results on the matching signals. The two accounts can use different sizes. The live journal links the exchange order to the signal timestamp and shows when fee totals were estimated.

While live entries are enabled and the account is flat, the worker verifies the current 15-minute contract as soon as it appears in the WTI tape. It refreshes the verified market rules about every 45 seconds and can reuse them for up to 90 seconds on the exact contract. Check **Contract check** in `/oil-live status` to see whether the rules are ready. A cold lookup, the account checks, and the book read share the signal deadline. Account position, open orders and buying power are checked together for each candidate.

The live worker starts watching quotes while it checks the account and contract. It keeps watching within the original six-second signal window if a fresh offer is above the limit or too small. It sizes any immediate-or-cancel entry using at most 25% of displayed size at the original limit or better, subject to the budget, fee reserve, and exchange minimum. The window can end earlier near market close. A live entry requires receipt within three seconds and an exchange timestamp no more than 15 seconds old. Live exits use a five-second exchange timestamp limit. The worker rechecks the current quote and size before submitting. The Discord channel explains price and liquidity skips and reports exchange-confirmed zero fills separately.

The VM coordinates public Polymarket US requests across its workers and dashboard. A public 429 starts a shared cooldown using `Retry-After` when available. While live entries are enabled, NFL paper starts at most one request every five seconds. With unresolved live exposure the interval is eight seconds. An NFL paper 429 adds another 20 seconds of pause for NFL paper after the shared cooldown. Other background traffic shares up to two starts per second while armed and about 1.3 while there is exposure. WTI 15-minute signal and capture reads have dedicated capacity. When Oil D is off and flat, normal background pacing resumes. Public cooldowns still apply to fresh book requests. Signed order reconciliation and cancellation can proceed during a public cooldown. [Polymarket US rate limits](https://docs.polymarket.us/api-reference/rate-limits)

## WebSocket live pilot

Select `stream_end_to_end` to generate live signals with the same Oil D evaluator used by the WebSocket shadow. The signal freezes its side and limit. Entry requires a later WebSocket frame for the same contract that passes the three-second receipt and 15-second exchange-age limits, depth and fee checks, and the existing account and contract checks. The $1 per-entry cap and $2 pilot loss limit apply. Stale or unavailable streaming data causes the entry to skip. Take-profit, stop, order reconciliation, and official settlement still manage filled positions. The protective exit book can fall back to REST under the existing five-second exchange-age limit.

Pause with `/oil-live off` and wait for a flat pilot before applying the update or switching modes. On the updated VM, this helper configures the mode and verifies a fresh worker heartbeat.

```bash
cd ~/nfl-fair-value
bash deploy/azure/enable-oil-d-websocket.sh
```

Use `--resume` to resume real-money entries immediately after verification. Check **Quote source** in `/oil-live status` for **WebSocket · signals and live entries**. The live journal records the signal and final quote sources. Confirmed order IDs and fills still come from the exchange. To return to the previous mode, pause and wait until flat, edit `WTI_LIVE_BOOK_MODE` in `.secrets/wti-live.env`, and recreate the worker as described below.

## Stream-first trial

`WTI_LIVE_BOOK_MODE=rest` is the default. It uses recorded REST quotes and allows one paced direct GET per signal when no fresh quote is available. The REST request must start after the signal becomes eligible, measured when the app dispatches the request after pacing. The WebSocket keeps recording for comparison.

`WTI_LIVE_BOOK_MODE=stream_first` uses the full market-data WebSocket for the exact active WTI contract. A fresh frame must arrive after the entry signal becomes eligible. If the stream has no fresh quote, the worker can use qualifying REST data within the same deadline. An explicit stream halt blocks new sends for that contract. A disconnect clears the in-memory quote. The freshness limits and budget are the same in both modes. For the API protocol, see [Polymarket US market WebSocket](https://docs.polymarket.us/api-reference/websocket/markets).

Run the read-only replay on the VM over an active WTI session before choosing the live quote source.

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-live-worker python -m backend.wti.entry_replay --hours 24
```

The replay counts each distinct signal once and looks for the first fresh quote with enough size at the original limit across the remaining entry window. It reports how much time was left and separates missing, stale, above-limit, and thin quotes. Add `--json` for per-signal results. The replay uses recorded books and estimates when displayed size was available. The live timing report and journal show actual submissions and fills.

To select the stream, pause with `/oil-live off` and wait until the account has no active live order or position. Add `WTI_LIVE_BOOK_MODE=stream_first` to the existing `.secrets/wti-live.env` on the VM, or update that setting if it is already present. Recreate the live worker, check **Quote source** in `/oil-live status`, and follow the resume command below when ready.

```bash
cd ~/nfl-fair-value
sudo docker compose up -d --no-deps --force-recreate --wait --wait-timeout 90 wti-live-worker
```

Use the same pause and recreate steps with `WTI_LIVE_BOOK_MODE=rest` to return to REST. The [WTI shadow report](WTI_REPLAY.md#polymarket-us-stream-shadow) shows stream capture and feed health.

## Oil D WebSocket entry shadow

The entry shadow starts with this release and runs while real-money entries are paused. It shares the existing authenticated market-data WebSocket and the paper desk’s WTI index data. It reads the contract’s price tick and minimum quantity from the public event data already collected by the desk. [Polymarket US Get Events](https://docs.polymarket.us/api-reference/events/get-events)

The shadow recomputes Oil D’s fair value and decision from a fresh WebSocket book for the current 15-minute contract. It freezes the side and limit, waits for the configured execution latency, and looks for a distinct later WebSocket quote within the original signal window. Each book must meet the three-second receipt and 15-second exchange-age limits. Simulated entry size uses at most 25% of displayed depth with a $1 cap including the fee reserve. The journal allows one simulated fill per contract. This report covers signals and simulated entries.

The report shows the shadow limit, fresh ask, estimated edge after model fees, and execution result. It includes the original paper limit and the ask for that side when a paper intent for the same contract was recorded in the preceding 15 seconds. The shadow selects its own side, so the two directions can differ. Market closures, stale inputs, missing increments, and thin or above-limit books can all produce skips.

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-live-worker python -m backend.wti.oil_d_shadow_store --hours 24 --limit 10
```

Add `--json` for timestamps, sizing details, and quote evidence. The journal is stored at `/data/wti/shadow/oil-d.sqlite3` on the predictions volume. To disable the shadow, set `WTI_OIL_D_SHADOW_ENABLED=false` in the VM’s `.secrets/wti-live.env` and follow the pause and recreate steps above.

## Oil D shadow exit comparison

Each new simulated WebSocket fill starts three separate exit simulations with the same contract, side, entry price, size, entry fees, and take-profit limit.

| Policy | When it exits |
| --- | --- |
| Current stop | Trigger the stop on the first fresh bid at or below the threshold. |
| 3-second stop | Trigger after qualifying fresh bids stay at or below the threshold for three seconds. |
| Take profit or settle | Leave the take-profit rule active and use the official payout for any remaining size. |

The report compares net P&L over entries with usable outcomes in all three policies. It also counts pending outcomes and insufficient quote coverage. Take-profit fills use fresh displayed bids at the limit and a conservative share of visible depth. Queue position and exchange fills are unavailable to the simulator. Live orders continue to use the current stop.

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-live-worker python -m backend.wti.oil_d_shadow_exits --hours 24
```

Add `--json` for per-entry results and exit details. The report covers shadow entries created after this release starts collecting them.

## Entry timing report

Run the report on the VM after active WTI sessions have produced signals.

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-live-worker python -m backend.wti.live_timing --hours 24
```

The live journal keeps timing records for evaluated entry attempts in `entry_timings`. The report shows timing and skip reasons, then compares recorded REST and streaming quotes at the same decision times using each attempt's exchange-age limit. Attempts recorded before the trial use the original five-second limit. Direct live REST responses appear in the attempt diagnostics. The tape can buffer stream updates, so this comparison estimates historical quote availability. The order response summary separates immediate fills, terminal zero fills, pending orders, and unconfirmed submissions. Add `--json --attempts` to export individual timing records.

## Oil D 15-minute execution paper account

The separate account uses a **$100** starting allocation, a **$10** entry cap (10%), and a **$20** realized loss limit (20%). The entry cap includes the fee reserve. The model’s stake sizing, visible depth, available buying power, and remaining loss allowance can reduce an order. Balance is the starting allocation plus closed-trade P&L, including existing paper trades. The account uses Oil D’s live WebSocket signal evaluator and allows one order attempt per interval and one unresolved position at a time.

New simulated entries require a recorded pause on live entries. A pending simulated entry can be canceled when live resumes. Filled paper positions continue through their exit or official settlement.

Simulated entries keep the original IOC limit and size. The account samples the WebSocket book after a modeled 150 ms transport delay and allows full, partial, or zero fills. It checks the signal and quote before dispatch and tracks consumed liquidity. The exit engine models a resting take-profit, cancellation before the stop IOC, and official settlement for unsold contracts. The report flags quote gaps and uses displayed bids as a conservative estimate for resting fills. Fees are modeled and rounded to cents.

Use the Discord commands in the bot’s configured server:

| Command | What it does |
| --- | --- |
| `/oilpaperlivetestchannel channel:#oil-paper-live-test` | Choose where to receive paper account status, orders, fills, and closed-trade results. |
| `/oilpaperlivetest status` | Show entry eligibility, starting budget, realized P&L, buying power, and any active position. |
| `/oilpaperlivetest graph time:24h` | Plot closed-trade P&L and the resulting balance against the $100 baseline. |

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-live-worker python -m backend.wti.oil_d_paper --hours 24
```

Add `--json` for order and fill details. The journal is stored at `/data/wti/paper-execution/paper.sqlite3` on the predictions volume. To disable this account, set `WTI_OIL_D_PAPER_ENABLED=false` in the VM’s `.secrets/wti-live.env` and follow the pause and recreate steps.

## Pausing and updating

Use `/oil-live off` before a future update and let the worker manage the current trade until its position, orders, and settlement are resolved. The updater checks the existing live journal before stopping services and blocks while entries are enabled or a live trade is unresolved. If status reports an unknown order, reconcile it against your Polymarket US orders and positions before restarting or replacing the worker. The bot can trigger a stop while exchange liquidity is too thin to fill it.

After an update completes, you can clear a Discord operator pause from the VM when the configured worker is healthy and flat. The worker retains automated loss and reconciliation pauses.

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-live-worker python -m backend.wti.live_worker --resume-operator-pause
```
