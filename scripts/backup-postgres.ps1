[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if (-not (Test-Path '.env')) { throw 'Missing .env' }
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outputPath = Join-Path $PSScriptRoot "..\backups\intraday-sentinel-$timestamp.sql"
New-Item -ItemType Directory -Force (Split-Path $outputPath) | Out-Null
docker compose exec -T postgres sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | Set-Content -Encoding utf8 $outputPath
if ((Get-Item -LiteralPath $outputPath).Length -eq 0) { throw 'Backup output was empty.' }
Write-Host "Backup written to $outputPath"
