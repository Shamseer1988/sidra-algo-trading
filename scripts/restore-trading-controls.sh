#!/bin/sh
# Put back a trading_controls profile saved by apply-trading-controls.sh.
#
#   scripts/restore-trading-controls.sh backups/trading_controls-20260916-2030.json
#
# A settings change that cannot be undone in one command is a settings change
# people hesitate to make and then hesitate to reverse. Only
# application_settings is touched.

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

BACKUP=$1
[ -n "$BACKUP" ] || { echo "Usage: $0 <backup-file>" >&2; exit 1; }
[ -f "$BACKUP" ] || { echo "No such file: $BACKUP" >&2; exit 1; }

# A backup taken when nothing was stored cannot be restored into a value.
grep -q 'NONE STORED' "$BACKUP" && {
  echo "That backup records that no profile was stored. Refusing to write it." >&2
  exit 1
}

run_sql() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -'
}

run_sql_value() {
  docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -f -'
}

echo "=== RESTORING FROM $BACKUP ==="
cat "$BACKUP"
echo
printf 'Type RESTORE to write this profile back, anything else to abort: '
read -r ANSWER
[ "$ANSWER" = "RESTORE" ] || { echo "Aborted. Nothing was written."; exit 1; }

{
  printf "\\\\set profile "
  tr -d '\n' < "$BACKUP" | sed "s/'/''/g" | sed "s/^/'/;s/\$/'/"
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
