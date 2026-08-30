[CmdletBinding()]
param([switch]$RemoveData)

$ErrorActionPreference = 'Stop'
if ($RemoveData) {
    Write-Warning 'This removes local PostgreSQL and Redis volumes. Back up first.'
    docker compose down --volumes
} else {
    docker compose down
}
