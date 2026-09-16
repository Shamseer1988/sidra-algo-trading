#!/bin/sh
# Write a complete trading_controls profile, sized for the capital you will
# actually trade.
#
#   scripts/apply-trading-controls.sh --dry-run     show the change, write nothing
#   scripts/apply-trading-controls.sh               back up, confirm, write, verify
#
# The profile below is for a 10,000 rupee account. Every value is visible here
# rather than computed, because a script that derives a risk budget from a
# formula is a script whose output nobody checks.
#
# WHY THESE NUMBERS
#
#   account_capital 10000, risk_per_trade_percent 1.0
#     100 rupees at risk per trade. With the observed average stop of 0.376% of
#     price, that is a position of about 26,600 rupees, which needs 2.7x of the
#     5x intraday leverage available.
#
#   maximum_open_positions 1, maximum_open_exposure_percent 80
#     Read the exposure percentage carefully: the risk engine computes
#     capital * percent * leverage / 100, so the leverage multiplier is applied
#     on top of it. At 5x, 80 percent means 4x the account, or 40,000 rupees.
#     One position at the observed 0.376% average stop is 26,600, so a second
#     does not fit. The previous 400 meant 20x the account, which was survivable
#     at 10,00,000 only because the position count bound first.
#
#     Both limits are set because they fail differently: the position count stops
#     a second trade regardless of its size, and the exposure limit stops one
#     oversized trade. Either alone leaves a gap.
#
#   maximum_daily_risk_percent 3.0
#     Three full stop-outs ends the day at 300 rupees down. The previous 8.0 was
#     set for data collection against a paper account; on real money it is a
#     third of the account in a bad week.
#
#   maximum_signals 8
#     Matches the observed generation rate, so the scanner is not the binding
#     constraint. Exposure will decline most of them; that is the point.
#
#   minimum_rr 1.5, minimum_score 60, min_stop_distance_percent 0.35
#     Unchanged, deliberately. Average favourable excursion currently runs
#     0.89 R to 1.54 R, so a target beyond 1.5 R would rarely be reached, and
#     widening the stop moves the target with it — halving the cost per R while
#     putting the target out of reach. The tight stop is what these strategies
#     trade; it is not a parameter to tune for cost.
#
# Changing these values starts a new experiment. Results gathered before the
# change describe a different system and must not be pooled with results after
# it, so the script prints the SINCE line to use with the daily report.
#
# Only application_settings is touched. No trade, order, fill or signal is
# modified, and nothing here can reach a broker.

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

[ -f .env ] || { echo "Missing .env in $PROJECT" >&2; exit 1; }

DRY_RUN=no
[ "$1" = "--dry-run" ] && DRY_RUN=yes

BACKUP_DIR="$PROJECT/backups"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_DIR/trading_controls-$STAMP.json"

run_sql() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -'
}

run_sql_value() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -f -'
}

echo "=== CURRENT ==="
printf "select coalesce((select jsonb_pretty(value::jsonb) from application_settings where key = 'trading_controls'), 'NONE STORED');\n" \
  | run_sql_value | tee /tmp/trading-controls-current.json

echo
echo "=== PROPOSED (10,000 rupee account) ==="
cat <<'PROFILE' | tee /tmp/trading-controls-proposed.json
{
  "account_capital": 10000.0,
  "risk_per_trade_percent": 1.0,
  "maximum_daily_risk_percent": 3.0,
  "maximum_open_positions": 1,
  "maximum_open_exposure_percent": 80.0,
  "maximum_signals": 8,
  "minimum_score": 60,
  "minimum_rr": 1.5,
  "volume_multiplier": 1.3,
  "retest_tolerance_percent": 0.15,
  "minimum_ema_spread_percent": 0.05,
  "stop_atr_multiple": 1.1,
  "min_stop_distance_percent": 0.35,
  "trade_start_time": "09:30",
  "trade_cutoff_time": "14:45",
  "intraday_leverage_enabled": true,
  "intraday_leverage_multiplier": 5.0
}
PROFILE

if [ "$DRY_RUN" = "yes" ]; then
  echo
  echo "Dry run. Nothing was written."
  exit 0
fi

echo
printf 'Type APPLY to write this profile, anything else to abort: '
read -r ANSWER
[ "$ANSWER" = "APPLY" ] || { echo "Aborted. Nothing was written."; exit 1; }

mkdir -p "$BACKUP_DIR"
cp /tmp/trading-controls-current.json "$BACKUP"
chmod 600 "$BACKUP"
echo "Previous profile saved to $BACKUP"

# Written as a parameter rather than interpolated into the statement, so the
# JSON never passes through shell or SQL quoting.
{
  printf "\\\\set profile "
  # psql :'profile' quoting needs the value on one line.
  tr -d '\n' < /tmp/trading-controls-proposed.json | sed "s/'/''/g" | sed "s/^/'/;s/\$/'/"
  printf "\n"
  cat <<'SQL'
insert into application_settings (key, value, created_at, updated_at)
values ('trading_controls', :'profile'::json, now(), now())
    on conflict (key) do update
       set value = excluded.value,
           updated_at = now();
SQL
} | run_sql

echo
echo "=== STORED ==="
printf "select jsonb_pretty(value::jsonb) from application_settings where key = 'trading_controls';\n" | run_sql_value

echo
echo "Applied. This starts a new experiment; earlier sessions were gathered under"
echo "a different account size and must not be pooled with what follows."
echo
echo "  SINCE=$(date -d tomorrow +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d) scripts/paper-report.sh"
echo
echo "To roll back:  scripts/restore-trading-controls.sh $BACKUP"
