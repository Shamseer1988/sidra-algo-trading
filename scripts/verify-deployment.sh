#!/bin/sh
# Read-only check that api, scanner-worker, swing-worker and web all run the
# code that is checked out here. Builds nothing, restarts nothing, prints no
# secret. Safe at any time, including during a session.
#
#   scripts/verify-deployment.sh          # table, exit 1 on a mismatch
#   scripts/verify-deployment.sh --json   # the same as JSON
#
# Over SSH (the absolute docker path is handled inside):
#   ssh nas "cd /volume1/docker/sidra-algo-trading && scripts/verify-deployment.sh"
#
# Details and the reasoning: apps/api/scripts/verify_deployment.py

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

exec python3 apps/api/scripts/verify_deployment.py --repo "$PROJECT" "$@"
