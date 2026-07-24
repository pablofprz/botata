# Snapshot consistente de posted/botata.db y subida a la nube vía rclone.
# Requiere rclone configurado con un remote (ver scripts/README.md).
# Uso:  .\scripts\db-backup.ps1                 (remote 'gdrive' por default)
#       .\scripts\db-backup.ps1 -Remote dropbox
param(
    [string]$Remote = "butterbotdb",
    [string]$RemotePath = "butterbot/butterbot.db",  # remote SIN renombrar: el Drive es storage, botata.db local ↔ butterbot.db remoto
    [string]$Instance = ""                            # dir de instancia; default ..\bots\botata-arg (reorg 2026-07-24)
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$py = Join-Path $repo "..\.venv\Scripts\python.exe"   # venv en la raíz del workspace (reorg 2026-07-24)
if (-not (Test-Path $py)) { $py = "python" }

if (-not $Instance) { $Instance = Join-Path $repo "..\bots\botata-arg" }
$Instance = Resolve-Path $Instance
$db   = Join-Path $Instance "posted\botata.db"
$snap = Join-Path $env:TEMP "botata.snapshot.db"
if (-not (Test-Path $db)) { throw "no existe la DB: $db" }

& $py (Join-Path $PSScriptRoot "db_snapshot.py") $db $snap
rclone copyto $snap "${Remote}:${RemotePath}" --progress
Remove-Item $snap -Force
Write-Host "backup OK -> ${Remote}:${RemotePath}"
