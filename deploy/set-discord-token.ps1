[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$secretDirectory = Join-Path $projectRoot '.secrets'
New-Item -ItemType Directory -Force -Path $secretDirectory | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$directoryAcl = Get-Acl -LiteralPath $secretDirectory
$directoryAcl.SetAccessRuleProtection($true, $false)
$directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
    $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $secretDirectory -AclObject $directoryAcl
$tokenSecret = Read-Host 'Discord bot token (hidden)' -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($tokenSecret)
try {
    $tokenText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer).Trim()
    if ([string]::IsNullOrWhiteSpace($tokenText)) { throw 'The token cannot be empty.' }
    [IO.File]::WriteAllText((Join-Path $secretDirectory 'discord_token.txt'), $tokenText, [Text.UTF8Encoding]::new($false))
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    $tokenText = $null
}
Write-Host 'Saved the token in the private .secrets directory.'
