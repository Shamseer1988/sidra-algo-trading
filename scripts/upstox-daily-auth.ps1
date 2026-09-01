[CmdletBinding()]
param(
    [string]$Code = $null,
    [switch]$Docker = $false
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path '.env')) {
    throw 'Create .env with Upstox credentials before executing automated authentication.'
}

Write-Host "Initiating Upstox daily automated authentication..." -ForegroundColor Cyan

if ((-not $Docker) -and (Test-Path ".\.venv\Scripts\python.exe")) {
    if ($Code) {
        .\.venv\Scripts\python.exe -m app.cli upstox-auto-auth --code $Code
    } else {
        .\.venv\Scripts\python.exe -m app.cli upstox-auto-auth
    }
} elseif (Get-Command docker -ErrorAction SilentlyContinue) {
    $runningApi = docker compose ps --services --filter "status=running" | Where-Object { $_ -eq "api" }
    if ($runningApi) {
        if ($Code) {
            docker compose exec api python -m app.cli upstox-auto-auth --code $Code
        } else {
            docker compose exec api python -m app.cli upstox-auto-auth
        }
    } else {
        throw "Docker API service is not running."
    }
} else {
    throw "Neither running docker api service nor .venv python found."
}
