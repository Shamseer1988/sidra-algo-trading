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
#   account_capital 10000, risk_per_trade_percent 1.8
#     180 rupees at risk per trade. At the observed 0.376% average stop that is a
#     position of 47,872 rupees, or 4.79x the account — which is the point of
#     asking for 5x. The hard ceiling is 1.88%: 5x on 10,000 is 50,000 of
#     exposure, and 50,000 at a 0.376% stop is 188 rupees of risk. Nothing larger
#     fits, whatever the setting says.
#
#     This is nearly double the previous 1.0%. On a system whose measured edge is
#     zero, double the risk loses roughly twice as fast. It is set here because
#     the daily limits below are meaningless at 100 rupees a trade: a 1,000 rupee
#     loss limit would need ten consecutive losses to bind, which cannot happen
#     at two or three trades a day.
#
#   maximum_open_positions 1, maximum_open_exposure_percent 100
#     The risk engine computes capital * percent * leverage / 100, so the
#     leverage multiplier is applied on top of the percentage. At 5x, 100 percent
#     is 50,000 rupees, the whole of what the broker extends. One position of
#     47,872 fits and a second does not. Both limits are set because they fail
#     differently: the count stops a second trade of any size, the exposure limit
#     stops one oversized trade. Either alone leaves a gap.
#
#   daily_loss_limit 1000, daily_profit_target 2000
#     The day stops when session P&L — realised plus open, after costs — reaches
#     either. At 180 rupees of risk a losing trade costs 250 net, so the loss
#     limit binds on the fourth loss; a winning trade makes 200 net, so the
#     profit target needs ten.
#
#     Be clear about what that means. At the current two to three signals a day
#     the best possible session is about 600 rupees and the worst about 750, so
#     the profit target will effectively never bind. Reaching 2,000 needs ten
#     winning trades in one session, which needs minimum_score back at 60 for the
#     volume and a near-perfect day besides: eight trades all winning is 1,602.
#
#   maximum_daily_trades 4, maximum_daily_risk_percent 10.0
#     Two to three trades a day, capped twice over so neither cap alone has to
#     hold. The signal cap stops the scanner producing a fourth; the risk budget
#     of 550 rupees is three trades at 180 and stops the risk engine reserving
#     one. Brokerage is a flat 20 rupees per order at this size, so a fourth
#     trade costs 40 rupees of certain charges against an uncertain edge.
#
#     This is a testing limit, not a permanent one. Raising it later is a one
#     line change to both values together — they must move together or the
#     tighter of the two silently becomes the real limit.
#
#     Note what it does to the daily limits above: at three trades the most a
#     session can lose is 750 rupees and the most it can make is about 600, so
#     neither the 1,000 loss limit nor the 2,000 target can bind. They are
#     harmless, and they become meaningful when the trade count rises.
#
#   minimum_rr 1.5, minimum_score 71, min_stop_distance_percent 0.35
#     minimum_score matches what apply-strategies.sh set on the strategies, so the
#     account-level filter cannot re-admit what the strategy filter rejected. The
#     other two are unchanged, deliberately. Average favourable excursion currently runs
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
echo "=== PROPOSED (10,000 rupee account, 5x, +2000 / -1000 daily) ==="
cat <<'PROFILE' | tee /tmp/trading-controls-proposed.json
{
  "account_capital": 10000.0,
  "risk_per_trade_percent": 2.5,
  "maximum_daily_risk_percent": 10.0,
  "daily_loss_limit": 1000.0,
  "daily_profit_target": 2000.0,
  "maximum_open_positions": 1,
  "maximum_open_exposure_percent": 100.0,
  "maximum_daily_trades": 4,
  "minimum_score": 71,
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
