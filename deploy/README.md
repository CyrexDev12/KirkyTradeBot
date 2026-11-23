# Discord bot and Azure deployment

The bot is configured for application `1572302708419985518` and server `1572300706478817441`. Its default alert time is 30 minutes before kickoff. Set the destination channel after deployment with `/alerts setup`.

**Updating the live Mexico Central VM:** use [UPDATE_AZURE.md](UPDATE_AZURE.md) and `deploy/azure/update.sh`. This preserves the existing Discord token and runtime data, updates all six services, and does not reprovision the VM. The instructions below primarily cover creating a new installation.

## Deploy yourself using Azure Cloud Shell

This route uses the Azure account you sign into through the portal. Build `.deploy/nfl-fair-value-azure.zip` with `python3 deploy/package.py --output .deploy/nfl-fair-value-azure.zip`. It contains the application and deployment files. The token is entered separately at a hidden prompt.

1. Open Azure Cloud Shell in Bash through the Azure portal. Use its upload control to upload `nfl-fair-value-azure.zip`.
2. Extract the package and list your subscriptions.

```bash
unzip nfl-fair-value-azure.zip -d nfl-fair-value
cd nfl-fair-value
az account list --query "[].{Name:name,ID:id,State:state}" -o table
```

3. Use the ID of the subscription that holds your credits. Preview the resource changes.

```bash
bash deploy/azure/deploy.sh YOUR_SUBSCRIPTION_ID --plan
```

4. Deploy with the same ID. The script validates the Azure template, prompts for your Discord bot token with input hidden, creates the VM, installs Docker, uploads the application, and starts the configured services.

```bash
bash deploy/azure/deploy.sh YOUR_SUBSCRIPTION_ID
```

The default new deployment uses Mexico Central (`mexicocentral`) and creates a resource group named `nfl-fair-value-rg` if it does not already exist, an Ubuntu 24.04 VM named `nfl-fair-value`, a `Standard_B2s_v2` size with 2 vCPUs and 8 GiB memory, a 128 GiB Standard SSD OS disk, a public IPv4 address, and a virtual network. Compute, disk, IP, and network use count toward your subscription's billing and credits. Check the estimate for this configuration in the Azure pricing calculator. School subscription policies and available regional quota determine whether deployment is permitted. [Azure VM specifications](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/bsv2-series), [Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/)

The preview creates the empty resource group and an SSH key if needed, validates the template with Azure, then runs Azure's what-if operation. A validation failure stops the script before VM deployment or the token prompt. Successful validation does not reserve VM capacity. The deployment opens SSH to the current Cloud Shell IPv4 address. The bot connects outward to Discord. The dashboard binds to the VM's loopback address. The script prints the SSH command when finished. [Azure template validation](https://learn.microsoft.com/en-us/cli/azure/deployment/group#az-deployment-group-validate)

To change supported deployment settings before running the script, set `NFL_RESOURCE_GROUP`, `NFL_AZURE_LOCATION`, `NFL_VM_SIZE`, or `NFL_SSH_KEY` in Cloud Shell. Reuse the same resource group for updates. The default SSH key is `~/.ssh/nfl-discord`. Preserve it for later administration.

## Retry after the region-policy error

The subscription's permitted regions reported on November 17, 2026 are `francecentral`, `belgiumcentral`, `germanywestcentral`, `canadacentral`, and `mexicocentral`. Mexico Central was the successful region and is now the default. The `bicep.use_binary_from_path` configuration message is informational; the original deployment blocker was `RequestDisallowedByAzure` for West US 2.

Upload the updated `nfl-fair-value-azure.zip` to Cloud Shell's home directory. At a **PowerShell** prompt such as `PS /home/luke/nfl-fair-value>`, run:

```powershell
unzip -o "$HOME/nfl-fair-value-azure.zip" -d "$HOME/nfl-fair-value"
Set-Location "$HOME/nfl-fair-value"
$env:NFL_AZURE_LOCATION = 'mexicocentral'
$subscriptionId = az account show --query id -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Select your school subscription before continuing.' }
bash deploy/azure/deploy.sh $subscriptionId --plan
```

If the preview succeeds and its changes are correct, deploy:

```powershell
bash deploy/azure/deploy.sh $subscriptionId
```

In **Bash** Cloud Shell, the equivalent region setting is `export NFL_AZURE_LOCATION=mexicocentral`. The Windows deployment script accepts `-Location mexicocentral`.

The updated scripts reuse the existing resource group, including one created in West US 2 by the failed attempt. Its metadata location can differ from the location of the VM and network resources. [Azure resource group locations](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/manage-resource-groups-portal#what-is-a-resource-group)

For future region-policy errors, check Azure Portal > Policy > Assignments and inspect the parameters of **Allowed resource deployment regions** or **Allowed locations**. The output of `az account list-locations` alone does not establish which regions your policy permits. Policy assignments can also be inspected with `az policy assignment list --disable-scope-strict-match --filter 'atScope()' --query '[].{Name:displayName,Parameters:parameters}' -o json`. [Azure policy assignments](https://learn.microsoft.com/en-us/cli/azure/policy/assignment#az-policy-assignment-list)

If the next error is about VM size or quota, inspect that separately. For example, `az vm list-skus --location canadacentral --resource-type virtualMachines --size Standard_B2as_v2 --all -o json` shows SKU restrictions, and `az vm list-usage --location canadacentral -o table` shows regional compute usage and limits. A permitted region alone does not guarantee VM capacity or quota. [VM SKU availability](https://learn.microsoft.com/en-us/cli/azure/vm#az-vm-list-skus), [VM quota](https://learn.microsoft.com/en-us/azure/virtual-machines/quotas)

## Retry after SkuNotAvailable

The reported `Capacity Restrictions: Standard_B2as_v2` error in Canada Central means the requested VM size cannot currently be allocated there. Changing the VM size or using another permitted region can resolve this. The VM size is configurable in the bundle already uploaded; changing it does not require uploading another ZIP. [Azure SKU availability troubleshooting](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/error-sku-not-available)

From PowerShell Cloud Shell, inspect alternatives for the active subscription:

```powershell
az vm list-skus --location canadacentral --resource-type virtualMachines --all --query "[?name=='Standard_B2s_v2' || name=='Standard_D2as_v5' || name=='Standard_D2s_v5'].{Size:name,Restrictions:restrictions}" -o json
```

These alternatives each have 2 vCPUs and 8 GiB of memory. `Standard_B2s_v2` uses burstable Intel CPUs; the D-series choices provide sustained CPU capacity and can have a higher running cost. Check the price for the selected region and size. [Bsv2 sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/bsv2-series), [Dasv5 sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dasv5-series), [Dsv5 sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series)

`Restrictions: []` means the SKU listing reports no restrictions for that entry. It does not reserve live capacity or guarantee sufficient quota. A `Location` restriction prevents using that size in the listed region; `Zone` restrictions apply to the listed zones. The current template does not pin the VM to a zone. Use the template preview to check a candidate before deployment.

For example, if `Standard_B2s_v2` has no reported restrictions:

```powershell
$env:NFL_AZURE_LOCATION = 'canadacentral'
$env:NFL_VM_SIZE = 'Standard_B2s_v2'
$subscriptionId = az account show --query id -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Select your school subscription before continuing.' }
bash deploy/azure/deploy.sh $subscriptionId --plan
```

If that preview succeeds and the proposed configuration is correct, deploy with `bash deploy/azure/deploy.sh $subscriptionId` in the same shell. The region and size environment variables apply to both commands. For the Windows script, use `-Location canadacentral -VmSize Standard_B2s_v2` on both preview and deployment commands.

If all three alternatives are restricted or still fail capacity validation, inspect these sizes plus the original size in the other permitted regions:

```powershell
foreach ($region in @('mexicocentral', 'francecentral', 'germanywestcentral', 'belgiumcentral')) {
    Write-Host "Region: $region"
    az vm list-skus --location $region --resource-type virtualMachines --all --query "[?name=='Standard_B2as_v2' || name=='Standard_B2s_v2' || name=='Standard_D2as_v5' || name=='Standard_D2s_v5'].{Size:name,Restrictions:restrictions}" -o json
    if ($LASTEXITCODE -ne 0) { throw "Could not query VM sizes in $region." }
}
```

Set `NFL_AZURE_LOCATION` and `NFL_VM_SIZE` to the selected pair and rerun the preview. These steps are for recovering the failed initial deployment. Changing regions does not migrate an existing running VM or its persisted predictions and bot state.

## Enable and verify Discord

The commands are already registered in the specified server. They respond while the bot process is connected.

1. Give the bot **View Channel**, **Send Messages**, **Embed Links**, and **Read Message History** in the destination text channel.
2. As a server member with **Manage Server**, run `/alerts setup channel:#general minutes:30`, or choose your preferred channel in the command options.
3. Run `/botstatus`, followed by `/weeklymatchup`. Click a game's button to see its predictions.

The bot uses the guilds intent. Its commands use Discord's interactions API, and privileged Message Content and Server Members intents can stay disabled. Its Discord Developer Portal **Interactions Endpoint URL** should be empty because this implementation receives interactions through the Gateway connection. Slash command names use lowercase as required by Discord. [Discord application commands](https://docs.discord.com/developers/interactions/application-commands), [Receiving interactions](https://docs.discord.com/developers/interactions/receiving-and-responding)

If you need to authorize the application again, use the server installation link with the permissions above. [Install the existing bot](https://discord.com/oauth2/authorize?client_id=1572302708419985518&scope=bot%20applications.commands&permissions=84992&guild_id=1572300706478817441)

| Command | Behavior |
| --- | --- |
| `/weeklymatchup [season] [week]` | Lists the week with a button for every game |
| `/matchup game_id` | Opens a matchup, with current-week autocomplete |
| `/nextgame` | Shows the next confirmed kickoff within seven days |
| `/botstatus` | Reports backend reachability, channel settings, scheduler check, and latest delivery |
| `/help` | Explains commands and model labels |
| `/alerts setup channel minutes` | Enables automatic posts for this server, with a 5–120 minute lead time |
| `/alerts disable` | Disables automatic posts for this server |

Command responses are visible to the person who requested them. Automatic alerts are visible in the configured channel. Matchup buttons expire after 15 minutes and can be refreshed by running the command again.

Each matchup includes the existing model's win probability and total forecast, Monte Carlo expected scores, home/away/tie probabilities, total interval, and the difference between both models' conditional home-win probabilities. Generation times and historical reconstruction labels identify the forecasts being shown. MC availability follows the existing four-game and residual-sample requirements, with regular-season coverage.

## Delivery behavior and persistence

The scheduler checks every minute and sends within the configured window before kickoff. The Azure deployment refreshes the schedule cache every 15 minutes. A schedule refresh overdue by 30 minutes pauses automatic posts. Kickoff changes and canceled, postponed, unconfirmed, or completed games are checked before sending.

A cold backend may need several minutes to download NFL data and fit the existing models. If those predictions are still missing, automatic posts wait until the final two minutes before kickoff, then explicitly show unavailable fields. MC early-season insufficiency and postseason exclusions are included as labeled unavailable states. An outage lasting through kickoff causes that game's automatic post to be missed.

The bot stores server settings, delivery attempts, the complete prediction payload used for each attempt, and sent message IDs in `/state/discord.sqlite3`. A recorded delivery suppresses later posts for that server and game, including schedule changes. An uncertain send is reconciled against the bot's own channel messages before retrying. Discord's enforced nonce check also protects recent retries. Read Message History is required for this recovery. [Discord message delivery and nonces](https://docs.discord.com/developers/resources/message#create-message)

Docker's `predictions` and `bot-state` named volumes persist on the VM's managed OS disk through process restarts and container rebuilds. Keep one backend and one bot process for this application and state store. A second independent installation would have its own delivery history. The SQLite files use the VM's filesystem. [SQLite filesystem guidance](https://sqlite.org/useovernet.html)

The VM's OS disk is retained when the VM alone is deleted. Deleting the resource group removes its resources, including that disk. Back up the managed disk or use Azure VM Backup when you need recovery from disk loss or accidental deletion.

## Operations

Connect with the SSH command printed by the deployment, then use these commands on the VM.

```bash
cd ~/nfl-fair-value
sudo docker compose ps
sudo docker compose logs --tail 100 backend bot paper-worker crypto-worker updown-worker gold-worker wti-worker natgas-worker
curl -fsS http://127.0.0.1:8000/healthz
```

The backend health check verifies the HTTP process. The bot health check verifies that its connected scheduler is checking in. Forecast availability is visible in matchup replies. A process exit triggers Docker's restart policy. A persistent unhealthy status requires investigation through the logs.

For an application update, follow [UPDATE_AZURE.md](UPDATE_AZURE.md). Use the existing-VM updater so the region, VM size, token, and persisted state are retained. After manually replacing a token file on the VM, recreate the bot container with `sudo docker compose up -d --no-deps --force-recreate --wait bot` so its secret mount is refreshed.

SSH is restricted to the source address used during deployment. If Cloud Shell gets a new public address, rerunning the deployment updates that rule. Network access to Discord, NFLverse/GitHub, Docker's package repository, Docker Hub, and Python's package index is required for setup and operation.

## Windows deployment alternative

The PowerShell script uses a signed-in Azure CLI session on your own computer. The token already saved by `deploy/set-discord-token.ps1` is uploaded over SSH, then installed on the VM as a private file mounted into the bot container. The token stays outside the container image and source-control files.

```powershell
az login
az account list --query "[].{Name:name,ID:id}" -o table
ssh-keygen -t ed25519 -f "$env:USERPROFILE/.ssh/nfl-discord"
.\deploy\azure\deploy.ps1 -SubscriptionId YOUR_SUBSCRIPTION_ID -AdminCidr YOUR_PUBLIC_IP/32 -SshPrivateKeyPath "$env:USERPROFILE/.ssh/nfl-discord" -PlanOnly
.\deploy\azure\deploy.ps1 -SubscriptionId YOUR_SUBSCRIPTION_ID -AdminCidr YOUR_PUBLIC_IP/32 -SshPrivateKeyPath "$env:USERPROFILE/.ssh/nfl-discord"
```

The script prints a dashboard tunnel command. Run it locally, then open `http://127.0.0.1:8000`. Install Azure CLI using Microsoft's supported Windows installer if needed. [Azure CLI for Windows](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-windows)

## Run locally

Run the API in one terminal and the bot in a second terminal from the repository directory.

```powershell
.venv\Scripts\python -m pip install -r requirements.txt -r bot/requirements.txt
.venv\Scripts\python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

```powershell
$env:DISCORD_TOKEN_FILE = "$PWD/.secrets/discord_token.txt"
$env:DISCORD_APPLICATION_ID = "1572302708419985518"
$env:DISCORD_GUILD_ID = "1572300706478817441"
$env:NFL_API_URL = "http://127.0.0.1:8000"
.venv\Scripts\python -m bot.discord_bot
```

Stop the local bot when switching to Azure so that commands and alerts have one active consumer.

## Verification

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest -q
docker compose config --quiet
az bicep build --file deploy/azure/main.bicep
```

Tests cover alert windows, postponed games, cold startup, multiple simultaneous kickoffs, multiple servers, stale schedules, restart recovery, uncertain sends, channel changes, permission enforcement, command definitions, and sports-only API responses.
