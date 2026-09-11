#!/bin/sh
# Timestamped, compressed PostgreSQL dump. POSIX-sh counterpart to backup-postgres.ps1,
# written for unattended use from Synology DSM Task Scheduler (run as root).
#
#   scripts/backup-postgres.sh
#
# Named Docker volumes live under /volume1/@docker, which Hyper Backup cannot select as a
# source, so these dumps are the only backup of the trading data and of the strategy and
# trading-control rows in application_settings. Point Hyper Backup at backups/.
#
# Task Scheduler runs with a minimal environment and no working directory, so PATH is set
# explicitly and every path is resolved from the script's own location.

set -e

PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin
export PATH

KEEP_DAYS=${KEEP_DAYS:-30}
MIN_BYTES=${MIN_BYTES:-10000}

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT"

[ -f .env ] || { echo "Missing .env in $PROJECT" >&2; exit 1; }

mkdir -p backups
LOG=backups/backup.log
STAMP=$(date +%Y%m%d-%H%M%S)
TMP="backups/.dump-$STAMP.sql"
OUT="backups/intraday-sentinel-$STAMP.sql.gz"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

log "starting"

if ! docker compose exec -T postgres sh -lc \
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$TMP" 2>> "$LOG"; then
    log "ERROR: pg_dump failed"
    rm -f "$TMP"
    exit 1
fi

# pg_dump can exit 0 after writing nothing useful; a schema-only dump already exceeds
# MIN_BYTES, so anything smaller means the dump is not worth keeping.
SIZE=$(wc -c < "$TMP")
if [ "$SIZE" -lt "$MIN_BYTES" ]; then
    log "ERROR: dump is only $SIZE bytes, refusing to keep it"
    rm -f "$TMP"
    exit 1
fi

# Compress into place only once the dump is known good, so a partial file never lands in
# backups/ looking like a usable one.
gzip -c "$TMP" > "$OUT"
rm -f "$TMP"
log "OK $OUT ($SIZE bytes uncompressed)"

find backups -name 'intraday-sentinel-*.sql.gz' -mtime "+$KEEP_DAYS" -delete

echo "Backup written to $PROJECT/$OUT"
