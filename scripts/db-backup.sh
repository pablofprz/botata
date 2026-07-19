#!/usr/bin/env bash
# Snapshot consistente de posted/botata.db y subida a la nube vía rclone (prod/Linux).
# Uso:  ./scripts/db-backup.sh [remote] [remote_path]
set -euo pipefail
REMOTE="${1:-butterbotdb}"
REMOTE_PATH="${2:-butterbot/butterbot.db}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
DB="$REPO/posted/botata.db"
SNAP="$(mktemp --suffix=.db)"
[ -f "$DB" ] || { echo "no existe la DB: $DB" >&2; exit 1; }

"$PY" "$REPO/scripts/db_snapshot.py" "$DB" "$SNAP"
rclone copyto "$SNAP" "${REMOTE}:${REMOTE_PATH}" --progress
rm -f "$SNAP"
echo "backup OK -> ${REMOTE}:${REMOTE_PATH}"
