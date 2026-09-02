[CmdletBinding()]
param([Parameter(Mandatory = $true)][ValidateScript({ Test-Path $_ })][string]$BackupPath)

$ErrorActionPreference = 'Stop'
Write-Warning 'Restore overwrites schema data in the configured local database.'
$confirmation = Read-Host "Type RESTORE to continue"
if ($confirmation -ne 'RESTORE') { throw 'Restore cancelled.' }
Get-Content -Raw $BackupPath | docker compose exec -T postgres sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
if ($LASTEXITCODE -ne 0) { throw "Restore failed with exit code $LASTEXITCODE." }
Write-Host 'Restore completed.'
