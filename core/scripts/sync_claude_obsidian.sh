#!/usr/bin/env bash
# Sincroniza chats de Claude -> vault de Obsidian (Linux prod / paridad).
# En prod ajustar VAULT y las rutas dentro de claude_to_obsidian.py.
# Agendar via cron, ej: 0 22 * * * $HOME/butterbot/scripts/sync_claude_obsidian.sh
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# venv en la raíz del workspace (reorg 2026-07-24)
py="$repo/../.venv/bin/python"
[ -x "$py" ] || py="python3"
exec "$py" "$repo/scripts/claude_to_obsidian.py" "$@"
