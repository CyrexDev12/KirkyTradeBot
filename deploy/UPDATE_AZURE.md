# Update the live Azure installation

The existing VM is `nfl-fair-value` in resource group `nfl-fair-value-rg`, subscription `25328a62-e79b-4f1e-aaf3-d94e1554430b`, region **Mexico Central**. Update it with `update.sh`. The updater discovers its actual region and size and does not deploy the Bicep template or replace the VM.

This release updates the weekly parlay board with two- through five-leg tickets, model probabilities, reference odds and payout multipliers. The board continues to use your configured parlay channel.

On an existing five-ticket board, the first two tickets stay in their slots. The next scan can replace the other two-leg tickets with qualifying longer tickets while every game on the original is still pregame. The post identifies that revision and the API retains the superseded tickets. Once a ticket has started, its picks and prices are frozen.

## Upload and start the update

1. Use the prepared `.deploy/nfl-parlay-odds-azure.zip`, or build it on your computer from the updated project.

   ```powershell
   .\.venv\Scripts\python deploy/package.py --output .deploy/nfl-parlay-odds-azure.zip
   ```

2. Open **Bash** Cloud Shell. Upload `nfl-parlay-odds-azure.zip` using **Manage files → Upload**. Extract it into a fresh source directory.

   ```bash
   nfl_update_dir="$(mktemp -d "$HOME/nfl-update.XXXXXX")"
   unzip -q "$HOME/nfl-parlay-odds-azure.zip" -d "$nfl_update_dir"
   cd "$nfl_update_dir"
   ```

3. The updater needs the SSH private key from the original VM deployment. If `~/.ssh/nfl-discord` still exists, it uses that file. If you downloaded the key before closing the old Cloud Shell, rename your local saved copy to `nfl-discord-key`, upload it, then run:

   ```bash
   chmod 600 "$HOME/nfl-discord-key"
   export NFL_SSH_KEY="$HOME/nfl-discord-key"
   ```

   Keep the private key private. An ephemeral Cloud Shell does not retain files from an ended session. If the original key is lost, see the recovery instructions below before running the update.

4. Start the update:

   ```bash
   if [[ -z "${NFL_SSH_KEY:-}" && -f "$HOME/.ssh/nfl-update-key" ]]; then export NFL_SSH_KEY="$HOME/.ssh/nfl-update-key"; fi
   bash deploy/azure/update.sh 25328a62-e79b-4f1e-aaf3-d94e1554430b
   ```

The script refreshes the existing `AdministratorSSH` rule to allow this Cloud Shell's current public IPv4 address, uploads the source, and starts a detached update job on the VM. Its progress streams into your terminal. The job continues if Cloud Shell disconnects. Keep your SSH key so you can reconnect later. If SSH already works and you need to retain the current rule, append `--skip-ssh-rule`.

You do not need to enter or reset the Discord token. The updater reads the existing token file on the VM and creates the new paper operator secret there only when it is missing. Neither secret is printed or included in the upload.

## What happens during the update

- The source archive includes a SHA-256 manifest and LF endings for shell scripts. After extraction, source directories receive mode `0755` so the container's nonroot user can read nested packages. Private staging directories and secrets keep their restricted permissions. It excludes local credentials, cached data, and virtual environments.
- New images build while the old services remain online. The updater checks imports for the API, Discord bot and both workers before stopping the running services.
- The updater stops the old services briefly to create consistent compressed backups of both named volumes, including SQLite journals, paper-account data, predictions, and Discord delivery records. Backup time depends on data size.
- Application source and containers are replaced while the `.env` file, `.secrets` directory, `nfl-fair-value_predictions` volume, and `nfl-fair-value_bot-state` volume are retained. Existing forecasts, channel settings, alert history, and paper balances are not reset.
- The backend and bot return first. On the first paper-trading update, the updater builds GB/MC uncertainty profiles from the existing GB calibration and MC seasons 2024, 2025, and 2026 before starting the worker. This step can take several minutes. The worker must not create its first weekly plan before those inputs exist.
- All four services must become healthy. The updater checks the BTC account and parlay API routes and verifies the bot can authenticate to the paper operator API.
- If activation fails, the updater attempts to restore the old source and saved image versions automatically. It retains live data; it does not roll back Discord delivery history or erase new paper-ledger records. Data backups remain available for manual recovery. Database changes in this release are additive. Future incompatible schema migrations require a separate recovery plan.

Backups are under `~/nfl-backups/<release-id>` on the VM, with private directory permissions. Keep enough free disk space for the new images, a copy of both data volumes, and the previous release. Old images and backups are retained rather than deleted automatically. A backup on the same VM protects an application update; copy it off the VM if you need recovery from disk or VM loss.

Docker volumes persist independently of containers. Avoid `docker compose down -v`, `docker volume prune`, or deleting the resource group when updating. [Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/#a-volumes-lifecycle)

## Verify and configure Discord

The last container table should show `backend`, `bot`, `paper-worker`, `crypto-worker`, and `updown-worker` as **healthy**, followed by `Update complete`.

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
```

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
sudo docker compose logs --no-color --tail 100 backend bot paper-worker crypto-worker updown-worker
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
