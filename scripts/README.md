# scripts/ — ops de datos y credenciales

Tooling para trabajar cross-compu sin meter secretos ni la DB en el repo.

## Modelo mental
- **La DB es source of truth y single-writer.** Hay UNA canónica (la de prod). Desde
  otra compu traés una **copia de trabajo**, no un segundo master. No corras el bot
  en dos lados contra copias distintas.
- **Credenciales y DB nunca van al repo** (ya están en `.gitignore`). Se sincronizan
  aparte: credenciales por el repo privado `butterbot-secrets`, la DB por rclone.

## Base de datos (rclone)

### Setup por compu (una vez)
1. Instalar rclone: https://rclone.org/downloads/  (Windows: `winget install Rclone.Rclone`)
2. Configurar el remote a tu nube con `rclone config` (OAuth en el browser). El remote
   actual se llama **`butterbotdb`** (Google Drive) — es el default de las scripts.
   El token queda en `rclone.conf` (local, nunca en git).

> **Config encriptado:** si le pusiste password al `rclone.conf`, el uso manual te lo
> pide por consola. Para el cron de prod, exportá `RCLONE_CONFIG_PASS` (o usá
> `--password-command`) para que corra sin prompt.

### Primer uso (semilla)
La primera vez, subí tu DB local para crearla en la nube:
```powershell
.\scripts\db-backup.ps1       # crea butterbot/butterbot.db en el Drive
```

### Uso diario
```powershell
# Windows (dev / otra compu)
.\scripts\db-restore.ps1      # bajar la copia canónica antes de trabajar
.\scripts\db-backup.ps1       # subir tus cambios cuando terminás
```
```bash
# Linux (prod)
./scripts/db-backup.sh        # típicamente en un cron: respalda la DP viva
```
- `db-backup` hace un **snapshot consistente** (`db_snapshot.py`, backup API de SQLite)
  antes de subir — seguro con el bot corriendo.
- `db-restore` respalda tu DB local (`.bak-<fecha>`) y limpia WAL/SHM viejos antes de
  bajar la nube. Pasá `-Remote <nombre>` si tu remote no se llama `butterbotdb`.

En prod, dejá `db-backup.sh` en un cron (ej. cada hora) para tener siempre la última
copia en la nube; desde el dev hacés `db-restore` y trabajás sobre eso.

## Sync de chats → vault de Obsidian
Importa conversaciones de Claude al vault (`vault/chats/`, dentro del repo) con frontmatter,
tags por keyword y wikilinks. Idempotente (manifest en `chats/.manifest.json`). Solo stdlib.

- **code**: lee los transcripts `.jsonl` de Claude Code
  (`~/.claude/projects/C--Users-pablo-Documents-butterbot/*.jsonl`) — automático, sin export manual.
- **web**: lee exports `.md` de claude.ai que dejes en `~/claude-exports/web/` (export manual).

```powershell
.\scripts\sync_claude_obsidian.ps1              # ambas fuentes
.\scripts\sync_claude_obsidian.ps1 --dry-run    # previsualizar
.\scripts\sync_claude_obsidian.ps1 --source code
```
```bash
./scripts/sync_claude_obsidian.sh               # Linux / prod
```

**Agendar (Windows Task Scheduler)** — diario 22:00, sin ventana:
```powershell
$act = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -File `"$HOME\Documents\butterbot\scripts\sync_claude_obsidian.ps1`""
$trg = New-ScheduledTaskTrigger -Daily -At 22:00
Register-ScheduledTask -TaskName "butterbot-sync-chats" -Action $act -Trigger $trg
```
En Linux (prod), cron: `0 22 * * * $HOME/butterbot/scripts/sync_claude_obsidian.sh`.

## Graphify (grafo del código)
Grafo AST del repo (tree-sitter, 0 tokens) para consultar estructura sin re-leer archivos.
Ya está instalado (`pip install graphifyy`, CLI en el PATH de pyenv). Output en
`graphify-out/` (gitignored).

```bash
graphify update .                 # (re)construye el grafo — AST puro, sin LLM
graphify query "cómo se arma el contexto de una reply"
graphify explain "GenerateReplyNode"
graphify path "LoadContextNode" "UpdateProfileNode"
```

> **Nota de permisos:** ejecutar `graphify` y `graphify install --platform claude`
> (que crea el skill `/graphify` en `~/.claude/skills/`) los bloquea el clasificador
> de auto-mode de Claude Code por ser paquete de terceros que modifica config global.
> Para habilitarlo, agregá una regla de permiso Bash (`Bash(graphify:*)`) en tu
> `settings.local.json`, o corré `graphify install --platform claude` a mano en una
> terminal. El grafo se puede reconstruir a mano cuando cambia el código, o vía
> `graphify hook install` (git hook post-commit).

## Credenciales (butterbot-secrets)
Ver el README del repo privado `butterbot-secrets` (repos hermanos):
```
Documents/
  butterbot/           <- este repo
  butterbot-secrets/   <- .env + config/instagram.json (privado)
```
`pull` hidrata las credenciales en butterbot; `push` guarda tus cambios locales.
