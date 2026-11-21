#!/usr/bin/env bash
# Run with bash from either Azure Cloud Shell shell.
set -euo pipefail
subscription="${1:?Usage: bash deploy/azure/deploy.sh SUBSCRIPTION_ID [--plan]}"
mode="${2:-deploy}"
if [[ "$mode" != "deploy" && "$mode" != "--plan" ]]; then
    echo "The second argument must be --plan or omitted." >&2
    exit 1
fi
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
resource_group="${NFL_RESOURCE_GROUP:-nfl-fair-value-rg}"
location="${NFL_AZURE_LOCATION:-mexicocentral}"
vm_size="${NFL_VM_SIZE:-Standard_B2s_v2}"
private_key="${NFL_SSH_KEY:-$HOME/.ssh/nfl-discord}"
admin_ip="$(curl -fsS https://api.ipify.org)"
if [[ ! "$admin_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Could not determine this Cloud Shell's IPv4 address." >&2
    exit 1
fi
if [[ ! -f "$private_key" ]]; then
    mkdir -p "$(dirname "$private_key")"
    ssh-keygen -q -t ed25519 -f "$private_key" -N ""
fi
az account set --subscription "$subscription"
# An existing group's metadata location can differ from its resources' region.
group_exists="$(az group exists --name "$resource_group" --output tsv)"
case "$group_exists" in
    true) echo "Reusing resource group $resource_group; deploying resources in $location." ;;
    false) az group create --name "$resource_group" --location "$location" --output none ;;
    *) echo "Could not determine whether resource group $resource_group exists." >&2; exit 1 ;;
esac
parameters=("sshPublicKey=$(cat "$private_key.pub")" "adminCidr=$admin_ip/32" "location=$location" "vmSize=$vm_size")
echo "Validating deployment in $location with VM size $vm_size..."
if ! az deployment group validate --resource-group "$resource_group" --template-file "$project_root/deploy/azure/main.bicep" --parameters "${parameters[@]}" --output none; then
    echo "Azure validation failed. No VM deployment was started and no bot token was requested." >&2
    echo "For a region-policy error, set NFL_AZURE_LOCATION to a region permitted by your subscription. See deploy/README.md." >&2
    echo "For SkuNotAvailable, check alternative sizes with az vm list-skus, set NFL_VM_SIZE, then rerun --plan. A permitted region can still lack VM capacity." >&2
    echo "For quota errors, check az vm list-usage --location $location -o table." >&2
    exit 1
fi
if [[ "$mode" == "--plan" ]]; then
    az deployment group what-if --resource-group "$resource_group" --template-file "$project_root/deploy/azure/main.bicep" --parameters "${parameters[@]}"
    exit 0
fi
umask 077
token_file="$(mktemp)"
trap 'rm -f "$token_file"' EXIT
read -rsp "Discord bot token (hidden): " discord_token
echo
if [[ -z "$discord_token" ]]; then
    echo "A Discord token is required." >&2
    exit 1
fi
printf '%s' "$discord_token" > "$token_file"
unset discord_token
address="$(az deployment group create --name nfl-discord --resource-group "$resource_group" --template-file "$project_root/deploy/azure/main.bicep" --parameters "${parameters[@]}" --query properties.outputs.publicAddress.value -o tsv)"
remote="azureuser@$address"
ssh_options=(-i "$private_key" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
ssh_ready=false
for attempt in $(seq 1 30); do
    if ssh "${ssh_options[@]}" "$remote" true >/dev/null 2>&1; then
        ssh_ready=true
        break
    fi
    sleep 10
done
if [[ "$ssh_ready" != true ]]; then
    echo "The VM is not reachable over SSH. Check its status and administrator IP rule." >&2
    exit 1
fi
ssh "${ssh_options[@]}" "$remote" 'sudo cloud-init status --wait'
ssh "${ssh_options[@]}" "$remote" 'umask 077; mkdir -p ~/nfl-upload ~/nfl-fair-value'
mkdir -p "$project_root/.deploy"
tar -czf "$project_root/.deploy/app.tar.gz" --exclude=__pycache__ -C "$project_root" backend bot frontend requirements.txt Dockerfile compose.yaml .dockerignore .env.example
scp "${ssh_options[@]}" "$project_root/.deploy/app.tar.gz" "$remote:nfl-upload/app.tar.gz"
scp "${ssh_options[@]}" "$token_file" "$remote:nfl-upload/token"
ssh "${ssh_options[@]}" "$remote" 'set -e; tar -xzf ~/nfl-upload/app.tar.gz -C ~/nfl-fair-value; cd ~/nfl-fair-value; mkdir -p .secrets; chmod 700 .secrets; sudo install -o 10001 -g 10001 -m 0400 ~/nfl-upload/token .secrets/discord_token.txt; rm ~/nfl-upload/token; test -s .secrets/paper_operator_token.txt || openssl rand -hex 32 | sudo install -o 10001 -g 10001 -m 0400 /dev/stdin .secrets/paper_operator_token.txt; sudo docker compose up --build -d --wait --wait-timeout 900; sudo docker compose ps'
echo "Deployed in $location. Use /alerts setup in Discord to choose the channel."
echo "Admin connection: ssh -i $private_key $remote"
