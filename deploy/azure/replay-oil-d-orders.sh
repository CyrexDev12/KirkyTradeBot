#!/usr/bin/env bash
# Run read-only order replay in the current Azure worker using a source-only Python archive.
set -euo pipefail

usage() {
    echo 'Usage: bash deploy/azure/replay-oil-d-orders.sh SUBSCRIPTION_ID REPLAY.pyz [--hours N] [--since UTC] [--until UTC] [--trade-ids 2,3] [--latency S] [--depth-share FRACTION] [--fee-coefficient RATE] [--json]'
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
if [[ $# -lt 2 ]]; then usage >&2; exit 2; fi
subscription="$1"
bundle="$2"
shift 2

replay_args=()
while (( $# )); do
    case "$1" in
        --hours|--since|--until|--trade-ids|--latency|--depth-share|--fee-coefficient)
            if [[ $# -lt 2 || "$2" == --* ]]; then
                echo "Missing value for $1" >&2; exit 2
            fi
            replay_args+=("$1" "$2")
            shift 2
            ;;
        --hours=*|--since=*|--until=*|--trade-ids=*|--latency=*|--depth-share=*|--fee-coefficient=*|--json)
            replay_args+=("$1")
            shift
            ;;
        --help|-h) usage; exit 0 ;;
        *) echo 'Unsupported replay option.' >&2; usage >&2; exit 2 ;;
    esac
done

resource_group="${NFL_RESOURCE_GROUP:-nfl-fair-value-rg}"
vm_name="${NFL_VM_NAME:-nfl-fair-value}"
key="${NFL_SSH_KEY:-$HOME/nfl-update-key}"
[[ -f "$key" ]] || key="$HOME/.ssh/nfl-update-key"
[[ -f "$key" && -f "$bundle" && -r "$bundle" ]] || { echo 'The saved SSH key file or replay bundle is missing.' >&2; exit 1; }
chmod 600 "$key"

address="$(az network public-ip show --subscription "$subscription" --resource-group "$resource_group" \
    --name "$vm_name-ip" --query ipAddress -o tsv)"
admin="$(az vm show --subscription "$subscription" --resource-group "$resource_group" \
    --name "$vm_name" --query osProfile.adminUsername -o tsv)"
[[ -n "$address" && "$admin" =~ ^[A-Za-z0-9_][A-Za-z0-9_-]*$ ]] || {
    echo 'VM connection details are unavailable.' >&2
    exit 1
}
admin_ip="$(curl --max-time 20 -4fsS https://api.ipify.org)"
python3 -c 'import ipaddress,sys; [ipaddress.IPv4Address(arg) for arg in sys.argv[1:]]' "$address" "$admin_ip"
az network nsg rule update --subscription "$subscription" --resource-group "$resource_group" \
    --nsg-name "$vm_name-nsg" --name AdministratorSSH --source-address-prefixes "$admin_ip/32" --output none

loader='from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
with TemporaryDirectory(prefix="oil-d-order-replay-") as staging:
    archive = Path(staging) / "replay.pyz"
    with archive.open("wb") as output:
        shutil.copyfileobj(sys.stdin.buffer, output)
    raise SystemExit(subprocess.call([sys.executable, str(archive), *sys.argv[1:]]))'
printf -v command '%q ' python -c "$loader" "${replay_args[@]}"
ssh -T -i "$key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 \
    -o ServerAliveInterval=30 "$admin@$address" \
    "cd /home/${admin}/nfl-fair-value && sudo -n docker compose exec -T wti-live-worker $command" < "$bundle"
