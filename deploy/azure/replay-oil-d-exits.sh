#!/usr/bin/env bash
# Run a bundled, read-only exit analysis against the journals in the existing worker.
set -euo pipefail
subscription="${1:?Usage: bash deploy/azure/replay-oil-d-exits.sh SUBSCRIPTION_ID REPLAY.pyz}"
bundle="${2:?Pass the replay .pyz from the toolkit.}"
resource_group="${NFL_RESOURCE_GROUP:-nfl-fair-value-rg}"
vm_name="${NFL_VM_NAME:-nfl-fair-value}"
key="${NFL_SSH_KEY:-$HOME/nfl-update-key}"
[[ -f "$key" ]] || key="$HOME/.ssh/nfl-update-key"
[[ -f "$key" && -r "$bundle" ]] || { echo 'The saved SSH key file or replay bundle is missing.' >&2; exit 1; }
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
python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$admin_ip"
az network nsg rule update --subscription "$subscription" --resource-group "$resource_group" \
    --nsg-name "$vm_name-nsg" --name AdministratorSSH --source-address-prefixes "$admin_ip/32" --output none

loader='
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
with TemporaryDirectory(prefix="oil-d-exit-replay-") as staging:
    archive = Path(staging) / "replay.pyz"
    archive.write_bytes(sys.stdin.buffer.read())
    result = subprocess.run([sys.executable, str(archive), "--detail"], check=False)
    raise SystemExit(result.returncode)
'
ssh -T -i "$key" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30 \
    "$admin@$address" "cd /home/${admin}/nfl-fair-value && sudo -n docker compose exec -T wti-live-worker python -c $(printf '%q' "$loader")" \
    < "$bundle"
