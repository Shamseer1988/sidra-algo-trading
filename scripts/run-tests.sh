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
# the run. The versions match the dev extra in pyproject.toml.
#
# DATABASE_URL is rebuilt inside the container from the one it was given, so the
# password never passes through this script or the shell's history.
docker compose run --rm --user root --entrypoint sh api -c '
  set -e
  pip install --quiet --disable-pip-version-check "pytest>=8.3,<9" "pytest-asyncio>=0.24,<1" >/dev/null
  DATABASE_URL="${DATABASE_URL%/*}/'"$TEST_DB"'"
  export DATABASE_URL
  alembic upgrade head >/dev/null
  exec python -m pytest "$@"
' sh "$@"
