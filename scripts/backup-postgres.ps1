[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if (-not (Test-Path '.env')) { throw 'Missing .env' }
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outputPath = Join-Path $PSScriptRoot "..\backups\intraday-sentinel-$timestamp.sql"
New-Item -ItemType Directory -Force (Split-Path $outputPath) | Out-Null
docker compose exec -T postgres pg_dump -U intraday_sentinel -d intraday_sentinel | Set-Content -Encoding utf8 $outputPath
Write-Host "Backup written to $outputPath"
