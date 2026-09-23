#!/bin/sh
# Put back a paper_strategies configuration saved by apply-strategies.sh.
#
#   scripts/restore-strategies.sh backups/paper_strategies-20260923-2030.json
#
# Only application_settings is touched.

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

BACKUP=$1
[ -n "$BACKUP" ] || { echo "Usage: $0 <backup-file>" >&2; exit 1; }
[ -f "$BACKUP" ] || { echo "No such file: $BACKUP" >&2; exit 1; }

grep -q 'NONE STORED' "$BACKUP" && {
  echo "That backup records that nothing was stored. Refusing to write it." >&2
  exit 1
}

run_sql() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -'
}

echo "=== RESTORING FROM $BACKUP ==="
head -40 "$BACKUP"
echo
printf 'Type RESTORE to write this configuration back, anything else to abort: '
read -r ANSWER
[ "$ANSWER" = "RESTORE" ] || { echo "Aborted. Nothing was written."; exit 1; }

{
  printf "\\\\set strategies "
  tr -d '\n' < "$BACKUP" | sed "s/'/''/g" | sed "s/^/'/;s/\$/'/"
  printf "\n"
  cat <<'SQL'
insert into application_settings (key, value, created_at, updated_at)
values ('paper_strategies', :'strategies'::json, now(), now())
    on conflict (key) do update
       set value = excluded.value,
           updated_at = now();
SQL
} | run_sql

echo
echo "=== STORED ==="
cat <<'SQL' | run_sql
select item->>'name' as name, item->>'strategy_type' as type,
       item->>'enabled' as enabled, item->>'minimum_score' as min_score
  from application_settings, jsonb_array_elements(value::jsonb) as item
 where key = 'paper_strategies' order by 2;
SQL
