#!/usr/bin/env bash
#
# Reconcile, arm and disarm live submission.
#
# These four operations have no buttons in the web terminal: the Live readiness
# screen reports the gates but does not drive them. Doing them by hand means a
# login, a cookie jar and a CSRF header, which is a bad thing to ask somebody to
# type correctly while a market is open. Hence this.
#
# It reads and writes nothing itself — every action is an HTTP call to the API
# you already run, performed as you, and audit-logged there under your user.
#
#   ./scripts/live-control.sh status     what the gates say, and whether armed
#   ./scripts/live-control.sh reconcile  compare broker state against ours
#   ./scripts/live-control.sh arm "reason"   arm for the configured window
#   ./scripts/live-control.sh disarm     revoke immediately
#
# Environment:
#   SIDRA_URL    API base, default http://127.0.0.1:8000
#   SIDRA_EMAIL  admin email; prompted if unset
#
# Disarming is always safe and always allowed, including when nothing is armed.
# If you are unsure about anything, disarm.

set -euo pipefail

BASE="${SIDRA_URL:-http://127.0.0.1:8000}/api/v1"
JAR="$(mktemp)"

die() { printf '%s\n' "$*" >&2; exit 1; }
command -v curl >/dev/null || die "curl is required"

login() {
  local email password
  email="${SIDRA_EMAIL:-}"
  if [ -z "$email" ]; then read -r -p "Admin email: " email; fi
  read -r -s -p "Password: " password; echo

  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' -c "$JAR" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${email}\",\"password\":\"${password}\"}" \
    "${BASE}/auth/login")"
  [ "$code" = "200" ] || die "Login failed (HTTP ${code}). Nothing was changed."
}

# The CSRF cookie has to be echoed back as a header on every write. This is the
# part that is easy to get wrong by hand and silently returns 403.
csrf() {
  awk '$6 == "csrf_token" { print $7 }' "$JAR" | tail -1
}

# Writes the body to stdout and leaves the HTTP status in LAST_STATUS. The
# status is checked rather than assumed: an arm that the server refuses returns
# a perfectly well-formed JSON body, and a script that printed "armed" after one
# would be the worst possible bug in this file.
# The status goes to a file rather than a variable because every way of reading
# `call` — a pipe into the formatter, a command substitution — runs it in a
# subshell, and a variable set there does not survive. Getting this wrong made
# an earlier version of this script print "armed" after a refusal, which is the
# worst bug this file could have.
STATUS_FILE="$(mktemp)"
trap 'rm -f "$JAR" "$STATUS_FILE"' EXIT

call() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -b "$JAR" -c "$JAR" -X "$method" -H 'Content-Type: application/json'
              -o /dev/stdout -w '%{stderr}%{http_code}')
  if [ "$method" != "GET" ]; then args+=(-H "X-CSRF-Token: $(csrf)"); fi
  if [ -n "$body" ]; then args+=(-d "$body"); fi
  curl "${args[@]}" "${BASE}${path}" 2>"$STATUS_FILE"
}

status_of() { cat "$STATUS_FILE" 2>/dev/null; }

pretty() {
  if command -v python3 >/dev/null; then python3 -m json.tool 2>/dev/null || cat; else cat; fi
}

action="${1:-status}"

case "$action" in
  status)
    login
    echo "--- readiness gates"
    call GET /live/readiness | pretty
    echo "--- activation"
    call GET /live-shadow/activation | pretty
    ;;

  reconcile)
    login
    echo "--- reconciling against the broker (reads only, submits nothing)"
    call POST /live-shadow/reconcile | pretty
    echo
    if [ "$(status_of)" = "200" ]; then
      echo "safe_to_trade must be true above, and the result is only valid for 15 minutes."
    else
      die "Reconciliation did not run (HTTP $(status_of)). Nothing is armed."
    fi
    ;;

  arm)
    reason="${2:-}"
    [ ${#reason} -ge 8 ] || die 'A reason of at least 8 characters is required: ./live-control.sh arm "first live session, supervised"'
    login
    echo "--- arming live submission"
    # Refused unless every other gate passes, so this doubles as the final check.
    call POST /live-shadow/activation "{\"reason\":$(printf '%s' "$reason" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" | pretty
    echo
    if [ "$(status_of)" = "200" ]; then
      echo "ARMED until the expiry above. It lapses on its own, so it does not have"
      echo "to be disarmed at the end of the day — though disarming is free."
    else
      die "NOT ARMED (HTTP $(status_of)). The blocking gates are named above."
    fi
    ;;

  disarm)
    login
    echo "--- disarming"
    call DELETE /live-shadow/activation | pretty
    echo
    [ "$(status_of)" = "200" ] || die "Disarm failed (HTTP $(status_of)). Check the API and retry."
    echo "Disarmed. Live submission is off until somebody arms it again."
    ;;

  *)
    die "Usage: $0 {status|reconcile|arm \"reason\"|disarm}"
    ;;
esac
