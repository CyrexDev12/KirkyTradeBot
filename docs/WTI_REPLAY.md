# WTI 15-minute order replay

The WTI worker records signals from its five model accounts A–E, including orders that end up with zero fills. A shared recorder saves one set of order-book observations for each listed 15-minute window and the official payout when it arrives. The tape starts with the first window observed after deployment and lives at `/data/wti/replay/tape.sqlite3`. The existing Azure exporter includes it in `--only wti`.

## Report

On the Azure VM:

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-worker python -m backend.wti.replay --hours 168
```

Use `--source rest` for the public REST snapshots or `--source stream` for the authenticated market feed. The default automatically uses the stream if the report period contains stream book updates. Each report uses one source throughout. Capture starts with REST snapshots on deployment, and takes roughly two seconds plus request time per snapshot. Add `--json` for machine-readable output or pass `--db PATH` to analyze an exported tape.

| Scenario | Time after recorded order eligibility | Available displayed depth |
| --- | ---: | ---: |
| Baseline | 0 additional seconds | 50% |
| Slower | 3 additional seconds | 50% |
| Thinner | 0 additional seconds | 25% |
| Slower + thinner | 3 additional seconds | 25% |

The current strategy sets order eligibility two seconds after the signal. The report counts filled attempts, fraction of requested contracts filled, and quantity-weighted fill price separately for buys and sells. Percentages use attempts with usable evidence. Each model's P&L covers the same completed, verifiable windows across all four scenarios and uses the captured fee coefficient. Remaining contracts use the official payout. An arrival after the order deadline counts as an unfilled attempt.

Matching uses the latest eligible book received by the simulated arrival time. Missing or stale books, stream gaps and public trades newer than the last book lead to an unverified attempt. A newer book restores coverage. Each scenario tracks its own inventory and consumed depth. An exit needs a prior position on the same side. These comparisons reuse recorded order signals and stake sizes. If the paper bot produced no follow-up exit signal, remaining replay inventory can still settle. The results measure sensitivity to execution assumptions on these signals. They do not estimate how the strategy would change its decisions or resize over time under a different account history.

## Oil D live exit analysis

```bash
docker compose exec -T wti-live-worker python -m backend.wti.live_exit_replay --hours 168 --detail
```

From a release that includes the analyzer, this command reads the live order journal and the WTI tape. It pairs official payouts with the original live entry side, fill price, fees, and quantity. The result shows realized P&L and the outcome of holding the same positions through settlement. The analysis preserves actual entry sizes even though a different exit policy would change the account budget for later entries.

For filled stop orders, the report compares the actual price with a stop threshold reconstructed from the entry price, configured stop fraction, and recorded tick size. It also checks the latest qualifying WebSocket bid at the recorded send time using 25% of displayed depth and the actual order limit. It requires coverage for the entire filled quantity. The report leaves prices unverified on stale, missing, or insufficient books. The reconstructed stop threshold and displayed book are reference prices. A live order can see different liquidity by the time it reaches the exchange.

## Oil D exit policy replay

```bash
docker compose exec -T wti-live-worker python -m backend.wti.live_exit_policy_replay --hours 168 --detail
```

The replay uses the same confirmed live entry size, fill price, fees, side, and contract for all policies. It compares the current stop with a three-second confirmation and a policy that keeps the take-profit target and settles any unsold contracts. The same take-profit threshold is used in all three policies. The confirmed stop requires distinct clean updates and resets across gaps, stale books, and a bid that moves back above the trigger. Use `--confirm-seconds` to test a different duration.

The comparison uses recorded WebSocket quotes, stock exit freshness limits, a modeled 250 ms stop latency, and a 25% share of displayed depth. It requires an executable bid at or above the target to credit a take-profit fill. Historical queue positions and the exact exchange fees on hypothetical fills are unavailable. The report compares all policies on the same set of covered trades and shows how the replayed current stop compares with the recorded live result. Pure holding to settlement is reported separately as a reference.

## Polymarket US stream shadow

In Compose, `wti-live-worker` opens the authenticated read-only market stream for the current WTI 15-minute contract already discovered by `wti-worker`. It records full books and public trades to the tape alongside REST, including while live entries are paused. All execution still uses REST. The shadow capture checks readiness, gaps, and quote ages and adds no REST requests. [Polymarket US authentication](https://docs.polymarket.us/api-reference/authentication) · [Market WebSocket](https://docs.polymarket.us/api-reference/websocket/markets)

After an active trading window, run the shadow report on the VM. It compares fresh quote availability and delay after Oil D buy signals. For JSON, append `--json`.

```bash
cd ~/nfl-fair-value
sudo docker compose exec -T wti-live-worker python -m backend.wti.shadow --hours 24
sudo docker compose exec -T wti-live-worker cat /data/wti/replay/shadow-health.json
```

The worker uses `WTI_REPLAY_KEY_ID` and `WTI_REPLAY_SECRET_KEY` from the existing `.secrets/wti-replay.env` if configured, then falls back to the live key inside the live container. The replay key can be configured on its own when running without live credentials. The prompts below save it on the VM. If replacing it on a running VM, pause entries and wait for live orders and exposure to resolve first.

```bash
cd ~/nfl-fair-value
(
  set -euo pipefail
  umask 077
  mkdir -p .secrets
  read -rp "Polymarket US key ID: " replay_id
  read -rsp "Polymarket US secret key: " replay_secret
  printf '\n'
  [[ -n "$replay_id" && -n "$replay_secret" ]]
  printf 'WTI_REPLAY_KEY_ID=%s\nWTI_REPLAY_SECRET_KEY=%s\n' "$replay_id" "$replay_secret" > .secrets/wti-replay.env
  sudo docker compose up -d --no-deps --force-recreate wti-live-worker
)
```

REST collection continues if the stream disconnects. A window needs to elapse before the report can count its signals reliably. Use several active sessions to measure stream coverage and compare gaps before considering an execution change.
