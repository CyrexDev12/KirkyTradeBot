#!/usr/bin/env bash
# Runs on the VM (deploy/azure/export.sh uploads and starts it). Snapshots the live data
# volumes into ~/nfl-exports/<id>/ without stopping any service.
#   bash export-on-vm.sh EXPORTER.py EXPORT_ID [--only LIST] [--csv]
set -euo pipefail
umask 077
exporter="$(realpath "${1:?Usage: bash export-on-vm.sh export_data.py EXPORT_ID [--only LIST] [--csv]}")"
export_id="${2:?Missing export id}"
shift 2
[[ "$export_id" =~ ^[A-Za-z0-9T._-]+$ ]] || { echo 'Invalid export id.' >&2; exit 1; }
vm_root="${NFL_DEPLOY_HOME:-$HOME}"
project=nfl-fair-value
image="${NFL_EXPORT_IMAGE:-$project-backend:latest}"
out_dir="$vm_root/nfl-exports/$export_id"
extra=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --only)
            [[ "${2:-}" =~ ^[A-Za-z0-9_,-]+$ ]] || { echo '--only takes a comma list such as trading,gold' >&2; exit 1; }
            extra+=(--only "$2"); shift 2 ;;
        --csv) extra+=(--csv); shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
for volume in "${project}_predictions" "${project}_bot-state"; do
    sudo docker volume inspect "$volume" > /dev/null || { echo "Volume $volume not found on this VM." >&2; exit 1; }
done
sudo docker image inspect "$image" > /dev/null || { echo "Image $image not found; has the app been deployed?" >&2; exit 1; }
mkdir -p "$out_dir"
archive="nfl-data-$export_id.tar.gz"
echo "Snapshotting live data (services keep running) into $out_dir/$archive ..."
# Disposable container: no network, read-only root filesystem, the exporter mounted read-only.
# The volumes are mounted writable only so SQLite can share the live databases' WAL index;
# the exporter opens every database read-only and writes nothing to the volumes.
sudo docker run --rm --network none --read-only --user 0 --tmpfs /tmp \
    --mount "type=volume,source=${project}_predictions,target=/source/predictions" \
    --mount "type=volume,source=${project}_bot-state,target=/source/bot-state" \
    --mount "type=bind,source=$exporter,target=/tool/export_data.py,readonly" \
    --mount "type=bind,source=$out_dir,target=/export" \
    --entrypoint python "$image" /tool/export_data.py \
    --source predictions=/source/predictions --source bot-state=/source/bot-state \
    --output "/export/$archive" --owner "$(id -u):$(id -g)" "${extra[@]}"
test -s "$out_dir/$archive" && test -s "$out_dir/$archive.sha256"
echo "EXPORT_READY $out_dir/$archive"
