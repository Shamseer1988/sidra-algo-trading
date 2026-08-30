[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if (-not (Test-Path '.env')) {
    throw 'Create .env from .env.example and set strong local secrets before starting.'
}
docker compose up --build -d
docker compose ps
Write-Host 'Intraday Sentinel is starting. Web: http://localhost  API docs: http://localhost:8000/docs'
