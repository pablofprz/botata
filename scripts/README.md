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

## Credenciales (butterbot-secrets)
Ver el README del repo privado `butterbot-secrets` (repos hermanos):
```
Documents/
  butterbot/           <- este repo
  butterbot-secrets/   <- .env + config/instagram.json (privado)
```
`pull` hidrata las credenciales en butterbot; `push` guarda tus cambios locales.
