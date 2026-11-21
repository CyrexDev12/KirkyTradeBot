[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$AdminCidr,
    [Parameter(Mandatory)][string]$SshPrivateKeyPath,
    [string]$ResourceGroup = 'nfl-fair-value-rg',
    [string]$Location = 'mexicocentral',
    [string]$VmSize = 'Standard_B2s_v2',
    [string]$TokenFile,
    [switch]$PlanOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$privateKey = (Resolve-Path -LiteralPath $SshPrivateKeyPath).Path
$publicKey = Get-Content -Raw -LiteralPath ($privateKey + '.pub')
if (-not $TokenFile) { $TokenFile = Join-Path $projectRoot '.secrets/discord_token.txt' }
if ($AdminCidr -notmatch '^[0-9.]+/32$') { throw 'AdminCidr must be your IPv4 address followed by /32.' }

function Invoke-Azure {
    & az @args
    if ($LASTEXITCODE -ne 0) { throw 'Azure CLI command failed.' }
}
function Invoke-Ssh {
    & ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -i $privateKey $remote @args
    if ($LASTEXITCODE -ne 0) { throw 'SSH command failed.' }
}
function Copy-Remote([string]$source, [string]$destination) {
    & scp -o StrictHostKeyChecking=accept-new -i $privateKey $source ("${remote}:" + $destination)
    if ($LASTEXITCODE -ne 0) { throw 'File upload failed.' }
}

Get-Command az, ssh, scp, tar -ErrorAction Stop | Out-Null
Invoke-Azure account set --subscription $SubscriptionId
# Reuse the group without trying to change its metadata location.
$groupExists = Invoke-Azure group exists --name $ResourceGroup --output tsv
switch ("$groupExists".Trim()) {
    'true' { Write-Host "Reusing resource group $ResourceGroup; deploying resources in $Location." }
    'false' { Invoke-Azure group create --name $ResourceGroup --location $Location --output none }
    default { throw "Could not determine whether resource group $ResourceGroup exists." }
}
$template = Join-Path $PSScriptRoot 'main.bicep'
$parameters = @("sshPublicKey=$($publicKey.Trim())", "adminCidr=$AdminCidr", "location=$Location", "vmSize=$VmSize")
Write-Host "Validating deployment in $Location with VM size $VmSize..."
try {
    Invoke-Azure deployment group validate --resource-group $ResourceGroup --template-file $template --parameters @parameters --output none
} catch {
    Write-Host 'For a region-policy error, choose a permitted region with -Location.'
    Write-Host 'For SkuNotAvailable, check alternatives with az vm list-skus, choose -VmSize, then rerun -PlanOnly. A permitted region can still lack VM capacity.'
    Write-Host "For quota errors, check az vm list-usage --location $Location -o table."
    throw 'Azure validation failed. No VM deployment was started. See deploy/README.md and the Azure error above.'
}
if ($PlanOnly) {
    Invoke-Azure deployment group what-if --resource-group $ResourceGroup --template-file $template --parameters @parameters
    return
}
if (-not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) {
    throw 'Save your Discord token with deploy/set-discord-token.ps1 first.'
}
$deployment = Invoke-Azure deployment group create --name 'nfl-discord' --resource-group $ResourceGroup --template-file $template --parameters @parameters --output json | ConvertFrom-Json
$address = $deployment.properties.outputs.publicAddress.value
$remote = "azureuser@$address"

# cloud-init installs Docker. This command returns when the VM is ready to build.
$sshReady = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    & ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -i $privateKey $remote true 2>$null
    if ($LASTEXITCODE -eq 0) { $sshReady = $true; break }
    Start-Sleep -Seconds 10
}
if (-not $sshReady) { throw 'The VM did not become reachable over SSH. Check its status and the administrator IP rule.' }
Invoke-Ssh 'sudo cloud-init status --wait'
Invoke-Ssh 'umask 077; mkdir -p ~/nfl-upload ~/nfl-fair-value'
$staging = Join-Path $projectRoot '.deploy'
New-Item -ItemType Directory -Force -Path $staging | Out-Null
$archive = Join-Path $staging 'nfl-fair-value.tar.gz'
& tar -czf $archive --exclude=__pycache__ -C $projectRoot backend bot frontend requirements.txt Dockerfile compose.yaml .dockerignore .env.example
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
Copy-Remote $archive 'nfl-upload/app.tar.gz'
Copy-Remote (Resolve-Path -LiteralPath $TokenFile).Path 'nfl-upload/token'
Invoke-Ssh 'set -e; tar -xzf ~/nfl-upload/app.tar.gz -C ~/nfl-fair-value; cd ~/nfl-fair-value; mkdir -p .secrets; chmod 700 .secrets; sudo install -o 10001 -g 10001 -m 0400 ~/nfl-upload/token .secrets/discord_token.txt; rm ~/nfl-upload/token; test -s .secrets/paper_operator_token.txt || openssl rand -hex 32 | sudo install -o 10001 -g 10001 -m 0400 /dev/stdin .secrets/paper_operator_token.txt; sudo docker compose up --build -d --wait --wait-timeout 900'
Invoke-Ssh 'cd ~/nfl-fair-value && sudo docker compose ps'
Write-Host "Deployed in $Location. Use /alerts setup in Discord to choose the channel."
Write-Host "Dashboard tunnel: ssh -i $privateKey -L 8000:127.0.0.1:8000 $remote"
