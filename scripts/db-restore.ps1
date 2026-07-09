# Trae la DB canónica desde la nube a posted/butterbot.db (respaldando la local).
# Uso:  .\scripts\db-restore.ps1                 (remote 'gdrive' por default)
param(
    [string]$Remote = "gdrive",
    [string]$RemotePath = "butterbot/butterbot.db"
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$db = Join-Path $repo "posted\butterbot.db"
New-Item -ItemType Directory -Force -Path (Split-Path $db) | Out-Null

if (Test-Path $db) {
    $bak = "$db.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Move-Item $db $bak
    Write-Host "DB local respaldada -> $bak"
}
# Limpiar WAL/SHM viejos para no mezclar estados de otra sesión.
Remove-Item "$db-wal", "$db-shm" -ErrorAction SilentlyContinue

rclone copyto "${Remote}:${RemotePath}" $db --progress
Write-Host "restore OK <- ${Remote}:${RemotePath}"
Write-Warning "SQLite es single-writer: no corras el bot en dos compus contra copias distintas. Esta es una copia de trabajo; la canónica es la de prod."
