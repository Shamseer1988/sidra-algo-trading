#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

echo "Initiating Upstox daily automated authentication..."

if [ -f "${ROOT_DIR}/docker-compose.yml" ] && command -v docker >/dev/null 2>&1; then
    docker compose exec api python -m app.cli upstox-auto-auth "$@"
elif [ -f "${ROOT_DIR}/.venv/bin/python" ]; then
    "${ROOT_DIR}/.venv/bin/python" -m app.cli upstox-auto-auth "$@"
else
    python3 -m app.cli upstox-auto-auth "$@"
fi
