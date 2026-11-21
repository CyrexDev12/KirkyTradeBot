#!/usr/bin/env bash
# Run in Bash Cloud Shell from the newly extracted source package.
set -euo pipefail
subscription="${1:?Usage: bash deploy/azure/update.sh SUBSCRIPTION_ID [--skip-ssh-rule]}"
option="${2:-}"
if [[ -n "$option" && "$option" != --skip-ssh-rule ]]; then
    echo "Only --skip-ssh-rule is supported as a second argument." >&2
    exit 1
fi
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
resource_group="${NFL_RESOURCE_GROUP:-nfl-fair-value-rg}"
vm_name="${NFL_VM_NAME:-nfl-fair-value}"
private_key="${NFL_SSH_KEY:-$HOME/.ssh/nfl-discord}"
for command in az ssh scp python3 curl; do command -v "$command" >/dev/null; done
if [[ ! -f "$private_key" ]]; then
    echo "Upload your saved VM private key, then set NFL_SSH_KEY to its path. See deploy/UPDATE_AZURE.md." >&2
    exit 1
fi
chmod 600 "$private_key"
az account set --subscription "$subscription"
az vm show --resource-group "$resource_group" --name "$vm_name" \
    --query '{VM:name,Region:location,Size:hardwareProfile.vmSize}' -o table
address="$(az network public-ip show --resource-group "$resource_group" --name "$vm_name-ip" --query ipAddress -o tsv)"
admin_user="$(az vm show --resource-group "$resource_group" --name "$vm_name" --query osProfile.adminUsername -o tsv)"
[[ -n "$address" && -n "$admin_user" ]] || { echo 'VM connection details are unavailable.' >&2; exit 1; }
if [[ "$option" != --skip-ssh-rule ]]; then
    admin_ip="$(curl --max-time 20 -4fsS https://api.ipify.org)"
    python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$admin_ip"
    echo "Allowing SSH from this Cloud Shell address: $admin_ip/32"
    az network nsg rule update --resource-group "$resource_group" --nsg-name "$vm_name-nsg" \
        --name AdministratorSSH --source-address-prefixes "$admin_ip/32" --output none
fi
remote="$admin_user@$address"
ssh_options=(-i "$private_key" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30)
ssh "${ssh_options[@]}" "$remote" 'sudo -n true && test -f ~/nfl-fair-value/compose.yaml'
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
archive="$project_root/.deploy/update-$release_id.tar.gz"
python3 "$project_root/deploy/package.py" --output "$archive"
ssh "${ssh_options[@]}" "$remote" 'umask 077; mkdir -p ~/nfl-upload'
scp "${ssh_options[@]}" "$archive" "$remote:nfl-upload/update-$release_id.tar.gz"
scp "${ssh_options[@]}" "$project_root/deploy/azure/update-on-vm.sh" "$remote:nfl-upload/update-$release_id.sh"
# No interactive prompts on the VM. The job survives a Cloud Shell disconnect.
pid="$(ssh "${ssh_options[@]}" "$remote" "umask 077; : > ~/nfl-upload/update-$release_id.log; nohup bash ~/nfl-upload/update-$release_id.sh ~/nfl-upload/update-$release_id.tar.gz > ~/nfl-upload/update-$release_id.log 2>&1 < /dev/null & echo \$!")"
[[ "$pid" =~ ^[0-9]+$ ]] || { echo 'Could not obtain the update process ID.' >&2; exit 1; }
echo "Update started on the existing VM. Log: ~/nfl-upload/update-$release_id.log"
echo "You may disconnect; the VM keeps applying the update. Reconnect to read that log if needed."
ssh "${ssh_options[@]}" "$remote" \
    "tail --pid=$pid -n +1 -f ~/nfl-upload/update-$release_id.log; test -f ~/nfl-upload/update-$release_id.tar.gz.status || exit 1; exit \$(cat ~/nfl-upload/update-$release_id.tar.gz.status)"
