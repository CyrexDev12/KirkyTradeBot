#!/usr/bin/env bash
# Pull a snapshot of the live data off the Azure VM. Run in Bash Cloud Shell (or any shell
# with the Azure CLI, ssh and the VM's private key), from the project source directory:
#
#   bash deploy/azure/export.sh SUBSCRIPTION_ID [--only trading,gold] [--csv] [--output DIR]
#                               [--keep-on-vm] [--skip-ssh-rule]
#
# Services keep running. The archive lands in DIR (default ~/nfl-exports) with a .sha256 file;
# in Cloud Shell, download it with Manage files -> Download. See deploy/EXPORT_AZURE.md.
set -euo pipefail
subscription="${1:?Usage: bash deploy/azure/export.sh SUBSCRIPTION_ID [--only LIST] [--csv] [--output DIR] [--keep-on-vm] [--skip-ssh-rule]}"
shift
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
resource_group="${NFL_RESOURCE_GROUP:-nfl-fair-value-rg}"
vm_name="${NFL_VM_NAME:-nfl-fair-value}"
private_key="${NFL_SSH_KEY:-$HOME/.ssh/nfl-discord}"
output_dir="$HOME/nfl-exports"
remote_args=()
skip_rule=false
keep=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --only)
            [[ "${2:-}" =~ ^[A-Za-z0-9_,-]+$ ]] || { echo '--only takes a comma list such as trading,gold' >&2; exit 1; }
            remote_args+=(--only "$2"); shift 2 ;;
        --csv) remote_args+=(--csv); shift ;;
        --output) output_dir="${2:?--output needs a directory}"; shift 2 ;;
        --keep-on-vm) keep=true; shift ;;
        --skip-ssh-rule) skip_rule=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
for command in az ssh scp python3 curl; do command -v "$command" >/dev/null; done
if [[ ! -f "$private_key" ]]; then
    echo "Upload your saved VM private key, then set NFL_SSH_KEY to its path. See deploy/UPDATE_AZURE.md." >&2
    exit 1
fi
chmod 600 "$private_key"
az account set --subscription "$subscription"
address="$(az network public-ip show --resource-group "$resource_group" --name "$vm_name-ip" --query ipAddress -o tsv)"
admin_user="$(az vm show --resource-group "$resource_group" --name "$vm_name" --query osProfile.adminUsername -o tsv)"
[[ -n "$address" && -n "$admin_user" ]] || { echo 'VM connection details are unavailable.' >&2; exit 1; }
if [[ "$skip_rule" != true ]]; then
    admin_ip="$(curl --max-time 20 -4fsS https://api.ipify.org)"
    python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$admin_ip"
    echo "Allowing SSH from this address: $admin_ip/32"
    az network nsg rule update --resource-group "$resource_group" --nsg-name "$vm_name-nsg" \
        --name AdministratorSSH --source-address-prefixes "$admin_ip/32" --output none
fi
remote="$admin_user@$address"
ssh_options=(-i "$private_key" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30)
ssh "${ssh_options[@]}" "$remote" 'sudo -n true && test -f ~/nfl-fair-value/compose.yaml'
export_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
ssh "${ssh_options[@]}" "$remote" 'umask 077; mkdir -p ~/nfl-upload'
scp "${ssh_options[@]}" "$project_root/deploy/export_data.py" "$remote:nfl-upload/export-$export_id.py"
scp "${ssh_options[@]}" "$project_root/deploy/azure/export-on-vm.sh" "$remote:nfl-upload/export-$export_id.sh"
quoted_args=""
if [[ ${#remote_args[@]} -gt 0 ]]; then quoted_args="$(printf ' %q' "${remote_args[@]}")"; fi
ssh "${ssh_options[@]}" "$remote" \
    "bash ~/nfl-upload/export-$export_id.sh ~/nfl-upload/export-$export_id.py $export_id$quoted_args"
archive="nfl-data-$export_id.tar.gz"
mkdir -p "$output_dir"
chmod 700 "$output_dir"
scp "${ssh_options[@]}" "$remote:nfl-exports/$export_id/$archive" "$remote:nfl-exports/$export_id/$archive.sha256" "$output_dir/"
if command -v sha256sum >/dev/null; then (cd "$output_dir" && sha256sum -c "$archive.sha256")
else (cd "$output_dir" && shasum -a 256 -c "$archive.sha256"); fi
chmod 600 "$output_dir/$archive" "$output_dir/$archive.sha256"
ssh "${ssh_options[@]}" "$remote" "rm -f ~/nfl-upload/export-$export_id.py ~/nfl-upload/export-$export_id.sh"
if [[ "$keep" != true ]]; then
    ssh "${ssh_options[@]}" "$remote" "rm -rf ~/nfl-exports/$export_id"
fi
python3 - "$output_dir/$archive" <<'PY'
import json, sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    manifest = json.load(archive.extractfile("manifest.json"))
dbs = [f for f in manifest["files"] if f["kind"] == "sqlite"]
print(f"{len(manifest['files'])} files, {len(dbs)} databases, {manifest['total_bytes'] / 1e6:.1f} MB uncompressed")
for f in dbs:
    print(f"  {f['path']}: " + ", ".join(f"{t} {n}" for t, n in f["tables"].items()))
PY
echo "Saved: $output_dir/$archive"
echo "Verified against its SHA-256. In Cloud Shell, use Manage files -> Download to copy it to your computer."
