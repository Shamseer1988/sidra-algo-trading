[CmdletBinding()]
param([Parameter(Mandatory = $true)][ValidateScript({ Test-Path $_ })][string]$BackupPath)

$ErrorActionPreference = 'Stop'
Write-Warning 'Restore overwrites schema data in the configured local database.'
$confirmation = Read-Host "Type RESTORE to continue"
if ($confirmation -ne 'RESTORE') { throw 'Restore cancelled.' }
Get-Content -Raw $BackupPath | docker compose exec -T postgres psql -U intraday_sentinel -d intraday_sentinel
Write-Host 'Restore completed.'
