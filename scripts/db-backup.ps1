# Snapshot consistente de posted/butterbot.db y subida a la nube vía rclone.
# Requiere rclone configurado con un remote (ver scripts/README.md).
# Uso:  .\scripts\db-backup.ps1                 (remote 'gdrive' por default)
#       .\scripts\db-backup.ps1 -Remote dropbox
param(
    [string]$Remote = "gdrive",
    [string]$RemotePath = "butterbot/butterbot.db"
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$db   = Join-Path $repo "posted\butterbot.db"
$snap = Join-Path $env:TEMP "butterbot.snapshot.db"
if (-not (Test-Path $db)) { throw "no existe la DB: $db" }

& $py (Join-Path $PSScriptRoot "db_snapshot.py") $db $snap
rclone copyto $snap "${Remote}:${RemotePath}" --progress
Remove-Item $snap -Force
Write-Host "backup OK -> ${Remote}:${RemotePath}"
