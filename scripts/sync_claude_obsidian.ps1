# Sincroniza chats de Claude -> vault de Obsidian (Windows dev).
# Corre el importador con el python del venv del proyecto.
# Agendar como tarea diaria (ver scripts/README.md, seccion "Sync de chats").
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py (Join-Path $repo "scripts\claude_to_obsidian.py") @args
