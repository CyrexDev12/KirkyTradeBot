# Export the live data from the Azure VM

`deploy/azure/export.sh` copies a snapshot of everything the bots have recorded off the VM. **No service stops.** It connects the same way the updater does, using the same SSH key and Cloud Shell steps as [UPDATE_AZURE.md](UPDATE_AZURE.md).

## Run it

In **Bash** Cloud Shell, from an extracted copy of the project (steps 1–3 of [UPDATE_AZURE.md](UPDATE_AZURE.md)):

```bash
bash deploy/azure/export.sh 25328a62-e79b-4f1e-aaf3-d94e1554430b
```

The archive lands in `~/nfl-exports/nfl-data-<time>.tar.gz` next to its `.sha256` file. The command checks the download against that checksum before it reports success. To get the file onto your computer, use **Manage files → Download** in Cloud Shell.

| Option | Effect |
|---|---|
| `--only LIST` | Export just part of the data. LIST is a comma list:<ul><li>a volume: `predictions` or `bot-state`</li><li>a bot's folder: `trading`, `crypto`, `updown`, `gold`, `wti` or `natgas`</li><li>`bot`: the Discord bot's state (channel settings, alert history, posts)</li></ul>Example: `--only gold,bot` or `--only wti --csv` for every oil model's trades. |
| `--csv` | Also write every database table as a CSV under `csv/`, readable in Excel or Sheets |
| `--output DIR` | Save somewhere other than `~/nfl-exports` |
| `--keep-on-vm` | Also keep the archive on the VM in `~/nfl-exports/<id>/`. By default it's deleted after a verified download. |
| `--skip-ssh-rule` | Don't update the `AdministratorSSH` firewall rule, for when SSH already works from this address |

Examples:

```bash
# Everything, plus CSVs of every table
bash deploy/azure/export.sh 25328a62-e79b-4f1e-aaf3-d94e1554430b --csv
# Just the gold desk's trades as CSV
bash deploy/azure/export.sh 25328a62-e79b-4f1e-aaf3-d94e1554430b --only gold --csv
```

## What's in the archive

The archive's top-level folders are the volumes:

- `predictions/` holds the `nfl-fair-value_predictions` volume:
  - model outputs and forecasts
  - `trading/`: the NFL paper account
  - `crypto/`, `updown/` and `gold/`: those desks' journals and health files
  - `wti/` and `natgas/`: the oil and gas desks, with one journal per model in `wti/models/A…E/` plus `info.sqlite3` (detected and announced news)
- `bot-state/` holds the `nfl-fair-value_bot-state` volume: the Discord bot's channel settings, alert history and post receipts.
- `csv/` appears with `--csv`: one CSV per database table.
- `manifest.json` lists every file with its size and SHA-256, and every database's tables with their row counts.

Unpack it with `tar -xzf nfl-data-<time>.tar.gz`.

## How it stays safe

- **Consistent while live.** Each SQLite database is copied with SQLite's online-backup API, so the copy is a consistent snapshot, including rows the bots just committed, even while they keep writing. Every exported database is a single self-contained file.
- **Read-only.** The VM runs the exporter in a disposable container from the app's own image:
  - no network and a read-only filesystem
  - the exporter script itself mounted read-only
  - every database opened read-only
- **No secrets.** The Discord token and operator secret live in `~/nfl-fair-value/.secrets`, outside the volumes, and are never exported. As an extra guard, any file that looks like a secret is skipped and listed in the manifest.
- **Private files.** Every file in the archive and on disk is mode `600`.
- **Nothing left behind.** The uploaded scripts are removed from the VM, along with the VM copy unless you pass `--keep-on-vm`.

Treat the archive as private: it contains your paper-account history and Discord server and channel IDs.
