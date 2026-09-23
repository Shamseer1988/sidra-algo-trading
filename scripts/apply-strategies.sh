#!/bin/sh
# Apply the two strategy changes the seven sessions of data actually support.
#
#   scripts/apply-strategies.sh --dry-run     show the change, write nothing
#   scripts/apply-strategies.sh               back up, confirm, write, verify
#
# WHAT THIS DOES, AND WHAT IT DOES NOT DO
#
# It does not make the strategies profitable. Across 55 signals the four
# strategies together have a gross expectancy of approximately zero — a 40% win
# rate at 1.5 RR is breakeven by construction — and measured costs are 0.41 R
# per trade at a 10,000 rupee account. Nothing in this file changes that. Both
# changes below remove signals that lose money faster than the rest; the
# remainder still loses money. Expect roughly -0.28 R per trade instead of
# -0.41. That is a slower bleed, not an edge.
#
# CHANGE 1: disable orb-retest-v1
#   14 signals, 28.6% win rate, -0.29 average R, -4.00 R total. Worst of the
#   four on every measure in every one of the seven sessions. Its average
#   favourable excursion is 0.56 R against a 1.5 R target, so the typical signal
#   does not travel a third of the way to where it needs to go. That is a
#   structural mismatch between the entry and the target rather than a run of
#   bad luck, and it is the one strategy the evidence can carry a decision on.
#
# CHANGE 2: minimum_score 60 -> 71 on the remaining three
#   Signals scoring 71-77 returned +0.13 average R with 1.05 average MFE.
#   Signals scoring 60-70 returned -0.02 average R with 0.78 average MFE. The
#   direction is consistent across win rate, average R and MFE. The magnitude is
#   not statistically significant on 53 observations (z of about 0.4 on the win
#   rates), so this is a judgement that the ordering is real, not a proven
#   result. It costs about 60% of signal volume.
#
# The 84-86 band in the report is two signals and carries no information; it is
# not the reason for the threshold.
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

DISABLE_TYPE=orb-retest-v1
NEW_MINIMUM_SCORE=71

BACKUP_DIR="$PROJECT/backups"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_DIR/paper_strategies-$STAMP.json"

run_sql() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -'
}

run_sql_value() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -f -'
}

echo "=== CURRENT ==="
cat <<'SQL' | run_sql
select item->>'name'            as name,
       item->>'strategy_type'   as type,
       item->>'enabled'         as enabled,
       item->>'minimum_score'   as min_score,
       item->>'minimum_rr'      as min_rr
  from application_settings, jsonb_array_elements(value::jsonb) as item
 where key = 'paper_strategies'
 order by 2;
SQL

printf "select coalesce((select jsonb_pretty(value::jsonb) from application_settings where key = 'paper_strategies'), 'NONE STORED');\n" \
  | run_sql_value > /tmp/paper-strategies-current.json

if grep -q 'NONE STORED' /tmp/paper-strategies-current.json; then
  echo
  echo "No paper_strategies row is stored, so the code defaults are in force and there is"
  echo "nothing to edit. Save the strategies once from the Strategies workspace first."
  exit 1
fi

echo
echo "=== PROPOSED ==="
echo "  disable            $DISABLE_TYPE"
echo "  minimum_score ->   $NEW_MINIMUM_SCORE on every strategy"
echo
echo "  Expected effect: average R per trade from about 0.00 to about +0.13 gross,"
echo "  against a measured cost of 0.41 R. Still negative. This reduces losses."

if [ "$DRY_RUN" = "yes" ]; then
  echo
  echo "Dry run. Nothing was written."
  exit 0
fi

echo
printf 'Type APPLY to write these changes, anything else to abort: '
read -r ANSWER
[ "$ANSWER" = "APPLY" ] || { echo "Aborted. Nothing was written."; exit 1; }

mkdir -p "$BACKUP_DIR"
cp /tmp/paper-strategies-current.json "$BACKUP"
chmod 600 "$BACKUP"
echo "Previous strategies saved to $BACKUP"

# Edits the stored array in place rather than replacing it, so every field this
# script does not name — ids, names, universes, session windows — is preserved
# exactly as configured.
{
  printf "\\\\set disable_type '%s'\n" "$DISABLE_TYPE"
  printf "\\\\set new_score '%s'\n" "$NEW_MINIMUM_SCORE"
  cat <<'SQL'
update application_settings
   set value = (
         select jsonb_agg(
                  jsonb_set(
                    case when item->>'strategy_type' = :'disable_type'
                         then jsonb_set(item, '{enabled}', 'false'::jsonb)
                         else item
                    end,
                    '{minimum_score}', to_jsonb(:'new_score'::int)
                  )
                  order by ordinality
                )
           from application_settings s2,
                jsonb_array_elements(s2.value::jsonb) with ordinality as t(item, ordinality)
          where s2.key = 'paper_strategies'
       )::json,
       updated_at = now()
 where key = 'paper_strategies';
SQL
} | run_sql

echo
echo "=== STORED ==="
cat <<'SQL' | run_sql
select item->>'name'            as name,
       item->>'strategy_type'   as type,
       item->>'enabled'         as enabled,
       item->>'minimum_score'   as min_score,
       item->>'minimum_rr'      as min_rr
  from application_settings, jsonb_array_elements(value::jsonb) as item
 where key = 'paper_strategies'
 order by 2;
SQL

echo
echo "Applied. This starts a new experiment; earlier sessions were gathered under a"
echo "different strategy set and must not be pooled with what follows."
echo
echo "  SINCE=$(date -d tomorrow +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d) scripts/paper-report.sh"
echo
echo "To roll back:  scripts/restore-strategies.sh $BACKUP"
