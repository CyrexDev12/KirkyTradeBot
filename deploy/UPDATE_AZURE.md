# Update the live Azure installation

The existing VM is `nfl-fair-value` in resource group `nfl-fair-value-rg`, subscription `25328a62-e79b-4f1e-aaf3-d94e1554430b`, region **Mexico Central**. Update it with `update.sh`. The updater discovers its actual region and size and does not deploy the Bicep template or replace the VM.

This release gives the independent [$100 Oil D execution paper account](../docs/OIL_LIVE.md#oil-d-15-minute-execution-paper-account) a $10 entry cap and a $20 realized loss limit. Existing account history and Discord routing carry forward.

## Optional replay of earlier live orders

Upload `.deploy/nfl-oil-d-order-replay.zip` and your saved `nfl-update-key` in Bash Cloud Shell. The replay reads the existing worker’s live journal and WTI tape. It can run while the current application is online.

```bash
(
  set -euo pipefail
  replay_dir="$(mktemp -d)"
  unzip -q "$HOME/nfl-oil-d-order-replay.zip" -d "$replay_dir"
  bash "$replay_dir/deploy/azure/replay-oil-d-orders.sh" \
    25328a62-e79b-4f1e-aaf3-d94e1554430b "$replay_dir/oil-d-order-replay.pyz" \
    --since 2026-11-25T07:00:00Z --until 2026-11-25T19:15:00Z \
    --trade-ids "$(seq -s, 2 30)"
)
```

This selects the earlier 28 filled entry orders plus the zero-fill order. The report compares recorded and modeled fill counts, contract quantity, price error, and fees. It flags missing evidence and labels resting target fill estimates. Use `--hours 24` instead of the three cohort options to replay recent orders.

## Upload and start the update

1. Use the prepared `.deploy/nfl-oil-d-paper-limits-azure.zip`, or build it on your computer from the updated project. On macOS or Linux, run:

   ```bash
   python3 deploy/package.py --output .deploy/nfl-oil-d-paper-limits-azure.zip
   ```

   On Windows PowerShell:

   ```powershell
   python deploy/package.py --output .deploy/nfl-oil-d-paper-limits-azure.zip
   ```

2. Run `/oil-live off` in Discord and wait for `/oil-live status` to show no active order or position.

3. Open **Bash** Cloud Shell. Upload `nfl-oil-d-paper-limits-azure.zip` and your saved `nfl-update-key` file using **Manage files → Upload**. An ephemeral Cloud Shell discards uploaded files when the session ends. If the private key is lost, use the recovery steps below.

4. Run the update with real-money entries paused. The paper account will be eligible to open simulated positions during that pause.

   ```bash
   (
     set -euo pipefail
     sub="25328a62-e79b-4f1e-aaf3-d94e1554430b"
     key_file="${NFL_SSH_KEY:-$HOME/nfl-update-key}"
     [[ -f "$key_file" ]] || key_file="$HOME/.ssh/nfl-update-key"
     chmod 600 "$key_file"
     release_dir="$(mktemp -d)"
     unzip -q "$HOME/nfl-oil-d-paper-limits-azure.zip" -d "$release_dir"
     cd "$release_dir"
     NFL_SSH_KEY="$key_file" bash deploy/azure/update.sh "$sub"
   )
   ```

5. After **Update complete**, run `/oilpaperlivetest status` and check for the $10 entry cap and $20 realized loss limit. Use these commands to choose a channel or inspect the chart:

   ```text
   /oilpaperlivetestchannel channel:#oil-paper-live-test
   /oilpaperlivetest graph time:24h
   ```

6. To resume the existing live pilot, verify its budget and fresh heartbeat in `/oil-live status` and run the resume command in Bash Cloud Shell. Resuming halts new simulated entries while the paper account finishes any existing position.

   ```bash
   az vm run-command invoke \
     --subscription 25328a62-e79b-4f1e-aaf3-d94e1554430b \
     --resource-group nfl-fair-value-rg --name nfl-fair-value \
     --command-id RunShellScript \
     --scripts 'cd /home/azureuser/nfl-fair-value && docker compose exec -T wti-live-worker python -m backend.wti.live_worker --resume-operator-pause' \
     --query 'value[].message' -o tsv
   ```

Check the new execution paper account from any Bash Cloud Shell session:

```bash
az vm run-command invoke \
  --subscription 25328a62-e79b-4f1e-aaf3-d94e1554430b \
  --resource-group nfl-fair-value-rg --name nfl-fair-value \
  --command-id RunShellScript \
  --scripts 'cd /home/azureuser/nfl-fair-value && docker compose exec -T wti-live-worker python -m backend.wti.oil_d_paper --hours 24' \
  --query 'value[].message' -o tsv
```

The script refreshes the existing `AdministratorSSH` rule to allow this Cloud Shell's current public IPv4 address, uploads the source, and starts a detached update job on the VM. Its progress streams into your terminal. The job continues if Cloud Shell disconnects. Keep your SSH key so you can reconnect later. If SSH already works and you need to retain the current rule, append `--skip-ssh-rule`.

You do not need to enter or reset the Discord token. The updater reads the existing token file on the VM and creates the new paper operator secret there only when it is missing. Neither secret is printed or included in the upload.

## What happens during the update

- The source archive includes a SHA-256 manifest and LF endings for shell scripts. After extraction, source directories receive mode `0755` so the container's nonroot user can read nested packages. Private staging directories and secrets keep their restricted permissions. It excludes local credentials, cached data, and virtual environments.
- New images build while the old services remain online. Before stopping them, the updater checks imports, initializes the WTI and natural gas desks with their default settings on disposable storage, and verifies that the Discord image can render a graph without a display.
- If an Oil D live worker is already installed, the updater checks that it has paused new entries and resolved any active trade before stopping services. See [how to pause the pilot](../docs/OIL_LIVE.md#pausing-and-updating).
- The updater stops the old services briefly to create consistent compressed backups of both named volumes, including SQLite journals, paper-account data, predictions, and Discord delivery records. Backup time depends on data size.
- Application source and containers are replaced while the `.env` file, `.secrets` directory, `nfl-fair-value_predictions` volume, and `nfl-fair-value_bot-state` volume are retained. Existing forecasts, channel settings, alert history, and paper balances are not reset.
- The backend and bot return first. A new model version triggers an immediate gradient model rebuild and versioned paper-model calibration. The updater waits for the current winner calibration and builds current gradient and Monte Carlo uncertainty profiles before starting the paper worker. This step can take several minutes. Prior forecasts and calibration artifacts stay available to the previous release during recovery.
- The eight existing services must become healthy. The updater verifies bot authentication, the BTC, Up/Down, Gold, WTI, natural gas, and parlay API routes, and registration of the player forecast, weekly picks, and graph endpoints. It checks the authenticated live status endpoint before starting `wti-live-worker`.
- If activation fails, the updater attempts to restore the old source and saved image versions automatically. It retains live data; it does not roll back Discord delivery history or erase new paper-ledger records. Data backups remain available for manual recovery. Database changes in this release are additive. Future incompatible schema migrations require a separate recovery plan.

Backups are under `~/nfl-backups/<release-id>` on the VM, with private directory permissions. Keep enough free disk space for the new images, a copy of both data volumes, and the previous release. Old images and backups are retained rather than deleted automatically. A backup on the same VM protects an application update; copy it off the VM if you need recovery from disk or VM loss.

Docker volumes persist independently of containers. Avoid `docker compose down -v`, `docker volume prune`, or deleting the resource group when updating. [Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/#a-volumes-lifecycle)

## Verify and configure Discord

The last container table should show `backend`, `bot`, `paper-worker`, `crypto-worker`, `updown-worker`, `gold-worker`, `wti-worker`, and `natgas-worker` as **healthy** and `wti-live-worker` as running, followed by `Update complete`.

In Discord, run:

```text
/botstatus
/health
/account
/weeklymatchup
/parlay
/crypto-account
/crypto-trades
/crypto-stats
/updown-account
/updown-stats
/gold-account
/gold-stats
/player-stats player:Josh Allen stat:passing
/pygraph dataset:team-points subject:BUF
```

`/account` should show `PAPER_ONLY` and $100 on the first initialization, or the existing paper balance on later updates. `/health` reports the worker timestamp and any market-data errors. Container health confirms running processes; it does not guarantee that every upstream feed or model is ready.

The initial paper plan waits for the 30-minute results-ingestion grace period and then scans the week's markets, including alternative totals. HTTP `429` responses delay this scan while the client honors the server's retry delay. `last_planning_run` remains empty until a plan is saved. The updated `/health` shows the planning stage, games completed, and books received; the worker log reports progress after each game and when the plan is published. A separate heartbeat keeps updating during network waits, while `last_loop` continues to mean a completed worker cycle. Persistent loop errors still fail container health. Each market book is fetched once per planning pass and shared by its two outcomes; activation always fetches a fresh book. A completed plan can legitimately contain zero qualifying trades.

As a member with **Manage Server**, run the following once, choosing existing text channels in Discord. Previously configured channels carry forward. BTC delivery starts with the next entry, exit, or skip. If a weekly board is still active, the bot also posts it in a newly selected parlay channel.

```text
/paper-channels setup crypto:#btc-paper parlay:#nfl-parlays
/paper-channels show
```

To change individual destinations, use these commands.

```text
/paper-channels set kind:picks channel:#general
/paper-channels set kind:updates channel:#general
/paper-channels set kind:reports channel:#general
/paper-channels set kind:parlay channel:#nfl-parlays
/paper-channels set kind:crypto channel:#btc-paper
/paper-channels set kind:gold channel:#gold-paper
/paper-channels set kind:players channel:#player-stats
```

The player board checks for picks every 15 minutes when a player channel is configured. It adds up to ten distinct picks from verified lines before kickoff. The first forecast request may take longer while NFLverse player and team data is downloaded and cached. Container health is independent of sports data and quote availability.

The bot needs View Channel, Send Messages, Embed Links, and Read Message History in each selected channel. The existing `/alerts setup` configuration is preserved; rerun it only if you want to change the channel or timing. Use `/why-no-trades` to inspect why a plan has no qualifying paper trades. A lack of picks alone is not a deployment failure.

## Reconnect, inspect logs, or recover

If an older updater failed with `ModuleNotFoundError: No module named 'backend.trading.api'` and restored the previous services, use the newly rebuilt ZIP and retry from a fresh extraction directory. The old updater extracted nested source directories as `0700`; Docker preserved those permissions on root-owned directories, making the modules inaccessible to the app user. This release fixes extraction permissions, enforces readable source in the images, and checks imports before stopping any live service. No token reset or new VM is needed.

From Bash Cloud Shell:

```bash
nfl_vm_ip="$(az network public-ip show --subscription 25328a62-e79b-4f1e-aaf3-d94e1554430b --resource-group nfl-fair-value-rg --name nfl-fair-value-ip --query ipAddress -o tsv)"
ssh -i "${NFL_SSH_KEY:-$HOME/.ssh/nfl-discord}" "azureuser@$nfl_vm_ip"
```

On the VM:

```bash
cd ~/nfl-fair-value
sudo docker compose ps -a
sudo docker compose logs --no-color --tail 100 backend bot paper-worker crypto-worker updown-worker gold-worker wti-worker natgas-worker
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/api/crypto/account
```

The updater prints a log path such as `~/nfl-upload/update-<release-id>.log`. Run `tail -n 100` on that exact path. The matching `.tar.gz.status` file contains `0` only after success; a nonzero value indicates failure, including an update that restored the old version. If the status file is absent, check the log: the job may still be running. Do not launch another update while one is active.

If automatic recovery fails, share the log's final error. The backup contains `source.tar.gz`, `rollback-images.yaml`, both data-volume archives, and any captured failure logs. Restoring a data backup can revert delivery history and paper records, so do it only as a deliberate recovery operation.

### Recover a lost SSH key

Create a replacement key in Bash Cloud Shell:

```bash
mkdir -p "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/nfl-discord-replacement"
cat "$HOME/.ssh/nfl-discord-replacement.pub"
export NFL_SSH_KEY="$HOME/.ssh/nfl-discord-replacement"
```

Use Azure Portal → Virtual machines → **nfl-fair-value** → **Reset password** → **Reset SSH public key**, username `azureuser`, and paste only the `.pub` value. This requires permission to update the VM's access configuration. Download the replacement private key securely before ending an ephemeral session. Then rerun the updater, which also refreshes the SSH source-IP rule. [Microsoft SSH-key recovery](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/troubleshoot-ssh-connection#reset-credentials)

## Validation of this package

Local tests cover the application and the deployment archive/update failure paths. A local Docker Engine and the school Azure subscription are not available to the preparing assistant, so successful container execution and the production update must be verified by the checks above in your account.

## Exporting data

To copy the live data off the VM without an update or any downtime, see [EXPORT_AZURE.md](EXPORT_AZURE.md).
