# Trae la DB canónica desde la nube a posted/botata.db (respaldando la local).
# Uso:  .\scripts\db-restore.ps1                 (remote 'gdrive' por default)
param(
    [string]$Remote = "butterbotdb",
    [string]$RemotePath = "butterbot/butterbot.db",  # remote SIN renombrar: el Drive es storage, botata.db local ↔ butterbot.db remoto
    [string]$Instance = ""                            # dir de instancia; default ..\bots\botata-arg (reorg 2026-07-24)
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $Instance) { $Instance = Join-Path $repo "..\bots\botata-arg" }
$Instance = Resolve-Path $Instance
$db = Join-Path $Instance "posted\botata.db"
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
