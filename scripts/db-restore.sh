#!/usr/bin/env bash
# Trae la DB canónica desde la nube a posted/butterbot.db (respaldando la local).
# Uso:  ./scripts/db-restore.sh [remote] [remote_path]
set -euo pipefail
REMOTE="${1:-butterbotdb}"
REMOTE_PATH="${2:-butterbot/butterbot.db}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DB="$REPO/posted/butterbot.db"
mkdir -p "$(dirname "$DB")"

if [ -f "$DB" ]; then
    BAK="$DB.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$DB" "$BAK"
    echo "DB local respaldada -> $BAK"
fi
rm -f "$DB-wal" "$DB-shm"

rclone copyto "${REMOTE}:${REMOTE_PATH}" "$DB" --progress
echo "restore OK <- ${REMOTE}:${REMOTE_PATH}"
echo "AVISO: SQLite es single-writer. Esta es una copia de trabajo; la canónica es la de prod." >&2
