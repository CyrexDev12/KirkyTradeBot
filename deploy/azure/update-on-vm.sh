#!/usr/bin/env bash
# Existing-VM updater. The Cloud Shell wrapper uploads and launches this script.
set -euo pipefail
umask 077
archive="$(realpath "${1:?Usage: bash update-on-vm.sh PACKAGE.tar.gz}")"
vm_root="${NFL_DEPLOY_HOME:-$HOME}"
app_dir="$vm_root/nfl-fair-value"
project=nfl-fair-value
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup="$vm_root/nfl-backups/$release_id"
stage="$vm_root/nfl-releases/$release_id"
managed=(backend bot frontend deploy scripts tests docs Dockerfile requirements.txt pytest.ini compose.yaml .dockerignore .env.example README.md release.json)
stopped=false
source_changed=false
compose=(sudo docker compose --project-name "$project" --project-directory "$app_dir" -f "$app_dir/compose.yaml")
new_compose=(sudo docker compose --project-name "$project" --project-directory "$stage" -f "$stage/compose.yaml")

finish() {
    result=$?
    trap - EXIT
    set +e
    if [[ "$result" -ne 0 && "$stopped" == true ]]; then
        echo 'Update failed. Saving logs and restoring the previous application images and source.' >&2
        "${compose[@]}" logs --no-color --tail 100 > "$backup/failed-containers.log" 2>&1
        "${compose[@]}" stop --timeout 60
        if [[ "$source_changed" == true ]]; then
            mkdir -p "$backup/failed-source"
            for item in "${managed[@]}"; do
                if [[ -e "$app_dir/$item" ]]; then mv "$app_dir/$item" "$backup/failed-source/$item"; fi
            done
            tar -xzf "$backup/source.tar.gz" -C "$app_dir"
        fi
        if "${compose[@]}" -f "$backup/rollback-images.yaml" up -d --no-build --force-recreate --remove-orphans --wait --wait-timeout 300; then
            echo 'Previous services restored. Live data was retained; the update did not succeed.' >&2
        else
            echo "Automatic recovery needs attention. Backup and logs: $backup" >&2
        fi
    fi
    if [[ "$result" -ne 0 ]]; then echo "Update stopped (exit $result). Backup/staging: $backup / $stage" >&2; fi
    printf '%s\n' "$result" > "$archive.status"
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ -f "$archive" && -f "$app_dir/compose.yaml" ]] || { echo 'Existing installation or package is missing.' >&2; exit 1; }
sudo -n true
exec 9>"$app_dir/.update.lock"
flock -n 9 || { echo 'Another update is already running.' >&2; exit 1; }
sudo test -s "$app_dir/.secrets/discord_token.txt" || { echo 'The existing Discord token file is missing.' >&2; exit 1; }
mkdir -p "$backup" "$stage"

# Extract only regular package files/directories and verify the source manifest.
python3 - "$archive" "$stage" <<'PY'
import hashlib, json, pathlib, sys, tarfile
archive, destination = sys.argv[1:]
root = pathlib.Path(destination)
with tarfile.open(archive) as package:
    for member in package.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe package member')
        if any(part in {'.env', '.secrets', '.git', '.deploy', 'data', '.venv'} for part in path.parts):
            raise SystemExit('Package contains private/runtime files')
    package.extractall(root, filter='data')
# data_filter intentionally ignores directory modes. With umask 077 they become
# 0700, then Docker COPY preserves those modes on root-owned nested directories.
# Make only the extracted source traversable by the nonroot container user;
# the private staging parent and the secrets linked below retain their modes.
for path in root.rglob('*'):
    if path.is_dir():
        path.chmod(0o755)
manifest = json.loads((root / 'release.json').read_text())
for name, expected in manifest['files'].items():
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise SystemExit('Invalid manifest path')
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('Package checksum mismatch: ' + name)
print('Verified package from source commit ' + manifest['source_commit'])
PY

# The new internal secret is generated once on the VM, never bundled or printed.
if ! sudo test -s "$app_dir/.secrets/paper_operator_token.txt"; then
    sudo sh -c 'umask 077; openssl rand -hex 32 > "$1"; chown 10001:10001 "$1"; chmod 400 "$1"' sh "$app_dir/.secrets/paper_operator_token.txt"
fi
ln -s "$app_dir/.secrets" "$stage/.secrets"
if [[ -f "$app_dir/.env" ]]; then ln -s "$app_dir/.env" "$stage/.env"; fi
"${new_compose[@]}" config --quiet

# Pin old image IDs before the build moves the latest tags.
old_services=()
while IFS= read -r service; do
    old_services+=("$service")
done < <("${compose[@]}" config --services)
printf 'services:\n' > "$backup/rollback-images.yaml"
backend_image=''
for service in "${old_services[@]}"; do
    container="$("${compose[@]}" ps -a -q "$service")"
    [[ -n "$container" ]] || { echo "Existing container missing: $service" >&2; exit 1; }
    image_id="$(sudo docker inspect --format '{{.Image}}' "$container")"
    rollback_tag="$project-$service:rollback-$release_id"
    sudo docker image tag "$image_id" "$rollback_tag"
    printf '  %s:\n    image: %s\n' "$service" "$rollback_tag" >> "$backup/rollback-images.yaml"
    if [[ "$service" == backend ]]; then
        backend_image="$image_id"
        volume="$(sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$container")"
        [[ "$volume" == "${project}_predictions" ]] || { echo 'Unexpected prediction volume; stopping to preserve live data.' >&2; exit 1; }
    elif [[ "$service" == bot ]]; then
        volume="$(sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/state"}}{{.Name}}{{end}}{{end}}' "$container")"
        [[ "$volume" == "${project}_bot-state" ]] || { echo 'Unexpected bot-state volume; stopping to preserve live data.' >&2; exit 1; }
    fi
done
[[ -n "$backend_image" ]] || { echo 'Existing backend image is missing.' >&2; exit 1; }
present=()
for item in "${managed[@]}"; do if [[ -e "$app_dir/$item" ]]; then present+=("$item"); fi; done
tar -czf "$backup/source.tar.gz" -C "$app_dir" "${present[@]}"

echo 'Building the new images while the existing services remain online...'
"${new_compose[@]}" build
echo 'Checking imports and energy desk startup before stopping live services...'
for service in backend bot paper-worker; do
    if [[ "$service" == bot ]]; then
        imports='import bot.discord_bot; import bot.paper; import bot.player_stats; from matplotlib.backends.backend_agg import FigureCanvasAgg; from matplotlib.figure import Figure; FigureCanvasAgg(Figure()).draw()'
    elif [[ "$service" == backend ]]; then
        # Build the desks with their production defaults using only disposable storage.
        imports='import backend.app; import backend.crypto.worker; import backend.updown.worker; import backend.gold.worker; import backend.natgas.worker; import backend.wti.replay; import backend.wti.replay_capture; import backend.wti.live_worker; import backend.wti.oil_d_shadow_exits; import backend.wti.oil_d_paper; import backend.wti.paper_execution_replay; from backend.wti.worker import Desk; from backend.wti.commodity import NATGAS; Desk(None); Desk(None, commodity=NATGAS)'
    else
        imports='import backend.app; import backend.trading.worker'
    fi
    # Isolated disposable containers: no live volumes, credentials, or network.
    sudo docker run --rm --network none --read-only \
        --tmpfs /data:uid=10001,gid=10001,mode=0700 \
        --tmpfs /state:uid=10001,gid=10001,mode=0700 --tmpfs /tmp \
        --entrypoint python "$project-$service:latest" \
        -c "import os; assert os.getuid() == 10001; $imports; print('Startup checks passed: $service')"
done

# An existing live worker must finish managing its position before its process is stopped.
if [[ " ${old_services[*]} " == *" wti-live-worker "* ]]; then
    if ! "${compose[@]}" exec -T backend python -c '
from backend.wti.commodity import WTI
from backend.wti.live_store import LiveStore, live_path
path = live_path(WTI.directory())
if not path.is_file():
    raise SystemExit("The Oil D live journal could not be found.")
status = LiveStore(path).status()
if status["active_trade"] is not None:
    raise SystemExit("Oil D has an active position or order.")
if status.get("worker_configured") and status.get("entries_configured") and not status["paused"]:
    raise SystemExit("Oil D live entries are enabled.")
'; then
        echo 'Update blocked before stopping services. Use /oil-live off, then wait for /oil-live status to show no active position or order. Retry the update when the live journal can be checked.' >&2
        exit 1
    fi
fi

echo 'Pausing services for a consistent backup of the databases and prediction files...'
stopped=true
"${compose[@]}" stop --timeout 60
for volume in "${project}_predictions" "${project}_bot-state"; do
    sudo docker run --rm --network none --user 0 \
        --mount "type=volume,source=$volume,target=/source,readonly" \
        --mount "type=bind,source=$backup,target=/backup" "$backend_image" \
        python -c 'import os,sys,tarfile; p="/backup/"+sys.argv[1]+".tar.gz"; a=tarfile.open(p,"w:gz"); a.add("/source",arcname="."); a.close(); os.chmod(p,0o600)' "$volume"
done
echo "Data backups saved in $backup"
mkdir -p "$backup/previous-source"
source_changed=true
for item in "${managed[@]}"; do
    if [[ -e "$app_dir/$item" ]]; then mv "$app_dir/$item" "$backup/previous-source/$item"; fi
    if [[ -e "$stage/$item" ]]; then cp -a "$stage/$item" "$app_dir/$item"; fi
done

# Recreate to remount secret files and refresh command registration. Keep volumes.
"${compose[@]}" up -d --no-build --force-recreate --wait --wait-timeout 900 backend bot crypto-worker updown-worker gold-worker wti-worker natgas-worker

# Prepare profiles before the first worker plan can freeze an uncalibrated slate.
if ! "${compose[@]}" exec -T backend python -c 'from backend.trading.inputs import uncertainty_profiles_current; raise SystemExit(not uncertainty_profiles_current())'; then
    echo 'Preparing paper-model uncertainty profiles. The API and Discord bot are online during this step.'
    "${compose[@]}" exec -T backend python -c 'import time; from backend.model import MODEL_VERSION, get_metrics
for attempt in range(90):
    try:
        metrics = get_metrics()
        if metrics.get("model_version") == MODEL_VERSION and metrics.get("calibration"): break
    except RuntimeError: pass
    if attempt % 6 == 0: print("Waiting for current gradient model calibration...", flush=True)
    time.sleep(10)
else: raise SystemExit("Model calibration is not ready; inspect backend logs and retry the update.")'
    "${compose[@]}" exec -T backend python -m backend.trading.cli uncertainty --mc-seasons 2024,2025,2026
fi
"${compose[@]}" exec -T backend python -c 'from backend.trading.inputs import load_uncertainty, uncertainty_profiles_current; p=load_uncertainty(); assert uncertainty_profiles_current() and ("GB","moneyline") in p and ("MC","moneyline") in p, "Required current uncertainty profiles missing"'
"${compose[@]}" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 900 paper-worker
"${compose[@]}" exec -T bot python -c 'import asyncio; from decimal import Decimal; from bot.api import PredictionAPI
async def check():
    api=PredictionAPI("http://backend:8000")
    try:
        await api.paper_outbox()
        await api.oil_live("/status")
        live = await api.oil_live("/events", after=0, limit=1)
        assert isinstance(live["events"], list) and isinstance(live["latest_id"], int)
        chart = await api.oil_live("/chart", time="24h")
        assert isinstance(chart["available"], bool)
        paper_test = await api.oil_paper_test("/status")
        assert paper_test["mode"] == "PAPER_ONLY" and Decimal(paper_test["starting_budget"]) == 100
        assert Decimal(paper_test["entry_cap"]) == 10 and Decimal(paper_test["loss_cap"]) == 20
        paper_events = await api.oil_paper_test("/events", after=0, limit=1)
        assert isinstance(paper_events["events"], list) and isinstance(paper_events["latest_id"], int)
        paper_chart = await api.oil_paper_test("/chart", time="24h")
        assert isinstance(paper_chart["available"], bool)
        print("Operator authentication, Oil D event feeds, and both P&L charts verified.")
    finally: await api.close()
asyncio.run(check())'
curl -fsS http://127.0.0.1:8000/api/crypto/account > /dev/null
curl -fsS http://127.0.0.1:8000/api/crypto/stats > /dev/null
curl -fsS http://127.0.0.1:8000/api/updown/account > /dev/null
curl -fsS http://127.0.0.1:8000/api/gold/account > /dev/null
curl -fsS http://127.0.0.1:8000/api/wti/models > /dev/null
curl -fsS http://127.0.0.1:8000/api/natgas/models > /dev/null
curl -fsS http://127.0.0.1:8000/api/paper/parlay > /dev/null
"${compose[@]}" exec -T backend python -c 'import json, urllib.request; paths = json.load(urllib.request.urlopen("http://127.0.0.1:8000/openapi.json", timeout=10))["paths"]; assert {"/api/players/projection", "/api/players/picks", "/api/graphs/team-points"} <= paths.keys()'
curl -fsS http://127.0.0.1:8000/healthz
"${compose[@]}" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 90 wti-live-worker
"${compose[@]}" ps
printf '\nUpdate complete. Backup: %s\n' "$backup"
echo 'In Discord, try /player-stats and /pygraph, and use /paper-channels set to choose the player picks channel.'
echo 'For Oil D live setup and status, see docs/OIL_LIVE.md and /oil-live status.'
