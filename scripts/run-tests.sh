#!/bin/sh
# Run the API test suite against a throwaway database:
#
#   scripts/run-tests.sh
#   scripts/run-tests.sh tests/test_apply_settings.py -v
#
# NEVER RUN pytest AGAINST THE TRADING DATABASE.
#
# The suite creates, updates and deletes rows in the same tables the live
# account's history lives in -- trade_records, live_order_submissions,
# market_candles, application_settings. Pointed at the production database it
# would not fail; it would pass, and take the record of every trade ever placed
# with it. So this script resolves its own URL by appending _test to the
# database name and refuses to run if the result is the one in .env.
#
# The production image is built without the dev extra (infra/docker/api.Dockerfile
# runs `pip install -e .`, not `.[dev]`), which is right -- a container that
# places real orders has no reason to carry a test runner. pytest is installed
# into a one-off container instead, which is discarded when the run ends. The
# containers serving the API and the scanner are never touched, so this is safe
# to run while they are up, including during a session.
#
# The first run creates the test database and costs an extra few seconds for the
# install and the migrations. Later runs reuse the database and only reinstall.

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

[ -f .env ] || { echo "Missing .env in $PROJECT" >&2; exit 1; }

# Read from .env rather than from a running container, so the guard below
# compares against what is configured, not against whatever a container happens
# to have been started with.
PROD_DB=$(grep -E '^POSTGRES_DB=' .env | head -1 | cut -d= -f2-)
[ -n "$PROD_DB" ] || { echo "POSTGRES_DB is not set in .env" >&2; exit 1; }

TEST_DB="${PROD_DB}_test"
if [ "$TEST_DB" = "$PROD_DB" ]; then
  echo "Refusing to run: the test database resolved to the trading database ($PROD_DB)." >&2
  exit 1
fi

echo "=========================================================================="
echo "API TEST SUITE"
echo "=========================================================================="
echo "  trading database : $PROD_DB   (untouched)"
echo "  test database    : $TEST_DB   (created, migrated, written to)"
echo

# No arguments means the whole suite, quietly. pyproject's testpaths points
# pytest at tests/ either way; -q is only so a passing run is one line.
[ $# -gt 0 ] || set -- -q

# CREATE DATABASE has no IF NOT EXISTS, and a second run must not fail, so the
# error is tolerated and the next step would fail loudly if it really is absent.
docker compose exec -T postgres sh -lc \
  "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c 'CREATE DATABASE $TEST_DB' 2>/dev/null" \
  >/dev/null 2>&1 || true

# --user root because the image runs as the unprivileged sidra user, which
# cannot install into site-packages. --rm because nothing here should outlive
# the run.
#
# The three packages are named rather than installed as `.[dev]`, which would
# be the obvious way to stay in step with pyproject.toml. An editable install
# writes egg-info into /app, and /app belongs to sidra while the service drops
# every capability (cap_drop: ALL in docker-compose.yml), so root here has no
# DAC_OVERRIDE and cannot write there. These are the dev extra minus httpx,
# already a runtime dependency, and ruff, which no test needs.
#
# -p no:cacheprovider for the same reason: pytest's cache wants a directory
# under /app and would otherwise warn twice on every run about not getting one.
#
# DATABASE_URL is rebuilt inside the container from the one it was given, so the
# password never passes through this script or the shell's history.
# APPLICATION_MODE and LIVE_TRADING_ENABLED are forced back to their defaults.
#
# The service takes env_file: .env, so a one-off container built from it
# inherits this deployment's attestations -- which today say LIVE and true.
# Several tests assert what an operator gets when they touch neither:
# Settings().application_mode == "PAPER", live_trading_enabled is False. Handed
# the live environment they fail, and they are right to: they were asking about
# the default and were answered with the deployment.
#
# Overriding them is not weakening the suite. It is giving it the controlled
# environment a test run is supposed to have, so that what passes today still
# passes on a machine configured differently, and so that going live does not
# silently turn four safety assertions red.
docker compose run --rm --user root \
  -e APPLICATION_MODE=PAPER -e LIVE_TRADING_ENABLED=false \
  --entrypoint sh api -c '
  set -e
  pip install --quiet --disable-pip-version-check --root-user-action=ignore \
    "pytest>=8.3,<9" "pytest-asyncio>=0.24,<1" "psutil>=5.9.0,<7" >/dev/null
  DATABASE_URL="${DATABASE_URL%/*}/'"$TEST_DB"'"
  export DATABASE_URL
  alembic upgrade head >/dev/null
  exec python -m pytest -p no:cacheprovider "$@"
' sh "$@"
