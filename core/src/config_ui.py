"""config_ui.py — panel local de configuración de botata (T22).

UI web servida SOLO en 127.0.0.1 para editar settings.json, credenciales (.env),
fuentes de noticias y toggles de skills sin tocar JSON a mano.

    python config_ui.py [--port 8787] [--no-browser]

Decisiones (ver docs/ARQUITECTURA.md §15):
- stdlib puro (http.server + ui/config.html con vanilla JS). Sin deps.
- Escrituras atómicas (tmp + os.replace) con backup `.bak` del estado anterior.
- Validación server-side ANTES de escribir; error → 400 con detalle, no toca nada.
- Credenciales write-only: la API jamás devuelve valores del .env, solo qué claves
  están seteadas. Input vacío = no tocar.
- Los cambios en settings/.env/news requieren REINICIAR el bot (se leen al import).
  Skills es hot-reload.

Esta es la UI del NÚCLEO; las futuras UIs por canal (Discord, etc.) serán
interfaces separadas (decisión del admin, 2026-07-12).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("botata.config_ui")

from tools import ALL_SCOPES
from skills import load_skills, _parse_frontmatter

from instance import instance_dir

REPO_DIR = Path(__file__).resolve().parent.parent  # src/ -> raíz del repo
BASE_DIR = instance_dir()      # T28c: la instancia a editar (default = raíz del repo)
UI_HTML = REPO_DIR / "ui" / "config.html"  # asset del motor, no de la instancia

ENV_KEYS = [
    "BSKY_PASSWORD", "MASTODON_ACCESS_TOKEN", "OPENROUTER_API_KEY", "LLM_API_KEY",
    "BRAVE_API_KEY",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "YOUTUBE_API_KEY",
    "IG_USERNAME", "IG_PASSWORD", "GOOGLE_OAUTH_ID", "GOOGLE_OAUTH_SECRET",
]

_CHANNELS = {"bluesky", "mastodon"}
_FEED_TYPES = {"list", "feed", "following", "local"}  # `local` = timeline local (Mastodon)
_POLICIES = {"conservative", "balanced", "active"}
_NEWS_MODES = {"comment", "post"}
_MCP_TRANSPORTS = {"stdio", "http"}
_MOOD_MODES = {"manual", "auto"}
_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_PREF_MODES = {"manual", "add_only", "full_auto"}
_PREF_KINDS = {"like", "dislike"}
_EVENT_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")


# ─── Validadores (puros: devuelven lista de errores) ─────────────────────────
def validate_settings(s: dict) -> list[str]:
    errs: list[str] = []
    for key in ("BOT_HANDLE", "ADMIN_HANDLE"):
        if not (isinstance(s.get(key), str) and s[key].strip()):
            errs.append(f"{key} es obligatorio")
    admins = s.get("ADMIN_HANDLES", [])
    if not (isinstance(admins, list)
            and all(isinstance(h, str) and h.strip() for h in admins)):
        errs.append("ADMIN_HANDLES debe ser una lista de handles no vacíos")
    channel = s.get("CHANNEL", "bluesky")
    if channel not in _CHANNELS:
        errs.append(f"CHANNEL inválido '{channel}' (usar {sorted(_CHANNELS)})")
    if s.get("LANGUAGE", "es") not in ("es", "en"):
        errs.append("LANGUAGE inválido (es|en)")
    if not isinstance(s.get("BOT_NAME", ""), str):
        errs.append("BOT_NAME debe ser un string")
    if channel == "mastodon" and not str(
            s.get("MASTODON_BASE_URL", "")).startswith(("http://", "https://")):
        errs.append("CHANNEL=mastodon requiere MASTODON_BASE_URL "
                    "(ej. https://mastodon.social)")
    if not isinstance(s.get("POLL_INTERVAL_SECONDS", 60), (int, float)):
        errs.append("POLL_INTERVAL_SECONDS debe ser numérico")

    for i, feed in enumerate(s.get("FEEDS", [])):
        tag = f"FEEDS[{i}]"
        if not feed.get("name"):
            errs.append(f"{tag}: falta name")
        if feed.get("type", "list") not in _FEED_TYPES:
            errs.append(f"{tag}: type inválido '{feed.get('type')}' (usar {sorted(_FEED_TYPES)})")
        if feed.get("type", "list") not in ("following", "local") and not feed.get("uri"):
            errs.append(f"{tag}: falta uri (obligatoria salvo type=following/local)")
        if feed.get("posting_policy", "balanced") not in _POLICIES:
            errs.append(f"{tag}: posting_policy inválida '{feed.get('posting_policy')}'")

    groups = s.get("USER_GROUPS", {})
    if not isinstance(groups, dict):
        errs.append("USER_GROUPS debe ser un objeto {grupo: [handles]}")
        groups = {}
    else:
        feeds_by_name = {f.get("name"): f for f in s.get("FEEDS", []) if f.get("name")}
        for gname, members in groups.items():
            if not (isinstance(gname, str) and gname.strip()):
                errs.append("USER_GROUPS: nombre de grupo vacío")
            if not (isinstance(members, list)
                    and all(isinstance(h, str) and h.strip() for h in members)):
                errs.append(f"USER_GROUPS.{gname}: debe ser una lista de handles no vacíos")
                continue
            for m in members:
                if not m.startswith("feed:"):
                    continue
                fname = m[len("feed:"):].strip()
                feed = feeds_by_name.get(fname)
                if feed is None:
                    errs.append(f"USER_GROUPS.{gname}: '{m}' no matchea ningún feed de FEEDS")
                elif feed.get("type", "list") not in ("list", "following"):
                    errs.append(f"USER_GROUPS.{gname}: '{m}' es type={feed.get('type')} — "
                                "solo list/following tienen membresía definida")

    for name, cfg in s.get("TOOLS", {}).items():
        bad = set(cfg.get("scopes", [])) - ALL_SCOPES
        if bad:
            errs.append(f"TOOLS.{name}: scope(s) inválido(s) {sorted(bad)}")
        tg = cfg.get("groups")
        if tg is not None:
            if not (isinstance(tg, list) and all(isinstance(g, str) for g in tg)):
                errs.append(f"TOOLS.{name}: groups debe ser una lista de nombres de grupo")
            else:
                unknown = set(tg) - set(groups)
                if unknown:
                    errs.append(f"TOOLS.{name}: grupo(s) desconocido(s) {sorted(unknown)} "
                                "(definilos en USER_GROUPS)")

    for name, cfg in s.get("TASKS", {}).items():
        if "interval_hours" in cfg and not isinstance(cfg["interval_hours"], (int, float)):
            errs.append(f"TASKS.{name}: interval_hours debe ser numérico")

    budget = s.get("BUDGET")
    if budget is not None:
        daily = budget.get("daily_usd", 1.0)
        if not isinstance(daily, (int, float)) or isinstance(daily, bool) or daily <= 0:
            errs.append("BUDGET.daily_usd debe ser un número > 0")
        for flag in ("enabled", "announce"):
            if flag in budget and not isinstance(budget[flag], bool):
                errs.append(f"BUDGET.{flag} debe ser booleano")

    moods = s.get("MOODS")
    if moods is not None:
        if "enabled" in moods and not isinstance(moods["enabled"], bool):
            errs.append("MOODS.enabled debe ser booleano")
        if moods.get("mode", "manual") not in _MOOD_MODES:
            errs.append(f"MOODS.mode inválido '{moods.get('mode')}' (manual|auto)")
        sus = moods.get("susceptibility", 0.5)
        if not isinstance(sus, (int, float)) or isinstance(sus, bool) or not (0 <= sus <= 1):
            errs.append("MOODS.susceptibility debe ser un número entre 0 y 1")
        hyst = moods.get("hysteresis_hours", 2)
        if not isinstance(hyst, (int, float)) or isinstance(hyst, bool) or hyst < 0:
            errs.append("MOODS.hysteresis_hours debe ser un número >= 0")
        if not isinstance(moods.get("default", ""), str):
            errs.append("MOODS.default debe ser un string (name de un mood, o vacío)")
        sched = (moods.get("manual") or {}).get("schedule") or {}
        bad_days = set(sched) - _WEEKDAYS
        if bad_days:
            errs.append(f"MOODS.manual.schedule: día(s) inválido(s) {sorted(bad_days)} "
                        "(usar mon..sun)")

    prefs = s.get("PREFS")
    if prefs is not None and prefs.get("mode", "manual") not in _PREF_MODES:
        errs.append(f"PREFS.mode inválido '{prefs.get('mode')}' (manual|add_only|full_auto)")

    errs += validate_mcp(s.get("MCP", {}))

    models = s.get("MODELS")
    if models:
        endpoints = set(models.get("endpoints", {}))
        aliases = models.get("aliases", {})
        for alias, chain in aliases.items():
            if not chain:
                errs.append(f"MODELS.aliases.{alias}: cadena vacía")
            for j, hop in enumerate(chain or []):
                if hop.get("endpoint") not in endpoints:
                    errs.append(f"MODELS.aliases.{alias}[{j}]: endpoint desconocido "
                                f"'{hop.get('endpoint')}'")
                if not hop.get("model"):
                    errs.append(f"MODELS.aliases.{alias}[{j}]: falta model")
        for role, alias in models.get("roles", {}).items():
            if alias not in aliases:
                errs.append(f"MODELS.roles.{role}: alias desconocido '{alias}'")
    return errs


def validate_mcp(mcp: dict) -> list[str]:
    errs = []
    for name, cfg in (mcp or {}).items():
        transport = cfg.get("transport", "stdio")
        if transport not in _MCP_TRANSPORTS:
            errs.append(f"MCP.{name}: transport inválido '{transport}'")
        elif transport == "stdio" and not cfg.get("command"):
            errs.append(f"MCP.{name}: transport stdio requiere command")
        elif transport == "http" and not cfg.get("url"):
            errs.append(f"MCP.{name}: transport http requiere url")
        bad = set(cfg.get("scopes", [])) - ALL_SCOPES
        if bad:
            errs.append(f"MCP.{name}: scope(s) inválido(s) {sorted(bad)}")
    return errs


def validate_news(news: list) -> list[str]:
    errs = []
    for i, src in enumerate(news or []):
        tag = f"news[{i}]"
        if not (src.get("url") or "").startswith(("http://", "https://")):
            errs.append(f"{tag}: url inválida")
        if not src.get("title"):
            errs.append(f"{tag}: falta title")
        if src.get("mode", "post") not in _NEWS_MODES:
            errs.append(f"{tag}: mode inválido '{src.get('mode')}' (comment|post)")
    return errs


# ─── Catálogo de tools del motor ─────────────────────────────────────────────
def tool_catalog(settings: dict) -> list[dict]:
    """Todas las tools que el motor registra (nombre/descripción/scopes/enabled),
    con la sección TOOLS del settings aplicada. Lazy-importa botata (pesado, ~seg
    la primera vez); best-effort: si algo falla, lista vacía y la UI degrada."""
    try:
        import botata
        reg = botata.build_tool_registry(settings.get("TOOLS", {}),
                                         bsky=None, router=None, mcp_config=None)
        return [{"name": t.name, "description": t.description,
                 "scopes": sorted(t.scopes), "enabled": t.enabled,
                 "groups": sorted(getattr(t, "groups", None) or [])}
                for t in reg._tools.values()]
    except Exception as e:
        log.warning("tool_catalog no disponible: %s", e)
        return []


# ─── Proceso del bot (start/stop/restart desde la UI) ────────────────────────
_BOT: dict = {"proc": None, "log": None}


def bot_status(base_dir: Path) -> dict:
    proc = _BOT.get("proc")
    running = proc is not None and proc.poll() is None
    log_path = base_dir / "bot.log"
    tail = ""
    if log_path.exists():
        try:
            tail = "\n".join(
                log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
        except Exception:
            pass
    return {"running": running, "pid": proc.pid if running else None, "log_tail": tail}


def bot_control(base_dir: Path, action: str, mode: str = "open") -> list[str]:
    proc = _BOT.get("proc")
    if action in ("stop", "restart") and proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        _BOT["proc"] = None
    if action in ("start", "restart"):
        if _BOT.get("proc") is not None and _BOT["proc"].poll() is None:
            return ["el bot ya está corriendo (pará primero)"]
        if mode not in ("open", "admin_only"):
            return ["mode inválido (open|admin_only)"]
        logf = open(base_dir / "bot.log", "a", encoding="utf-8")
        _BOT["log"] = logf
        _BOT["proc"] = subprocess.Popen(
            [sys.executable, "-m", "botata", "--instance", str(base_dir), "--mode", mode],
            stdout=logf, stderr=subprocess.STDOUT)
    elif action == "stop":
        pass
    elif action != "restart":
        return ["action inválida (start|stop|restart)"]
    return []


# ─── Store: lectura/escritura atómica de los archivos de config ──────────────
class ConfigStore:
    """Paths inyectables (tests usan un tmp_path con fixtures)."""

    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.settings_path = self.base / "config" / "settings.json"
        self.news_path = self.base / "config" / "news_sites.json"
        self.env_path = self.base / ".env"
        self.skills_dir = self.base / "skills"
        self.moods_dir = self.base / "moods"
        self.db_path = self.base / "posted" / "botata.db"

    def _db(self):
        """Conexión al DB del bot (WAL: convive con el bot corriendo). Lazy
        import para que la UI arranque aunque falte sqlite-vec."""
        import db as dbmod
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return dbmod, dbmod.init_db(self.db_path)

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    # -- lectura ---------------------------------------------------------------
    def read_all(self) -> dict:
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        news = (json.loads(self.news_path.read_text(encoding="utf-8"))
                if self.news_path.exists() else [])
        soul_path = self.base / "context" / "SOUL.md"
        scrape = self.base / "scrape"
        try:  # indicador de Membrilla: cuántos sidecars dejó en la carpeta
            scrape_items = sum(1 for _ in scrape.rglob("*.json")) if scrape.is_dir() else 0
        except OSError:
            scrape_items = 0
        return {
            "settings": settings,
            "news": news,
            "env_keys": self._env_status(),
            "skills": self._skills_info(),
            "moods": self._moods_info(),
            "prompts": self._prompt_files(),
            "soul": soul_path.read_text(encoding="utf-8") if soul_path.exists() else "",
            "tools_catalog": tool_catalog(settings),
            "instance": {"path": str(self.base), "name": self.base.name,
                         "scrape_dir": str(scrape), "scrape_items": scrape_items},
        }

    def _moods_info(self) -> list[dict]:
        """Moods con cuerpo completo (editables desde la UI)."""
        out = []
        if not self.moods_dir.is_dir():
            return out
        for path in sorted(self.moods_dir.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if parsed is None or not parsed[0].get("name"):
                continue
            meta, body = parsed
            out.append({
                "name": meta["name"],
                "description": meta.get("description", ""),
                "enabled": meta.get("enabled", "true").lower() != "false",
                "body": body.strip(),
                "file": path.name,
            })
        return out

    _MOOD_NAME_RE = re.compile(r"^[a-z0-9_-]{2,30}$")

    def edit_mood(self, body: dict) -> list[str]:
        """Crea/edita/borra un mood (archivo moods/<name>.md)."""
        name = (body.get("name") or "").strip().lower()
        if not self._MOOD_NAME_RE.match(name):
            return ["name inválido (minúsculas/números/guiones, 2-30 chars, en inglés "
                    "por convención: upbeat, gloomy...)"]
        self.moods_dir.mkdir(parents=True, exist_ok=True)
        path = self.moods_dir / f"{name}.md"
        if body.get("action") == "delete":
            if not path.exists():
                return [f"no existe el mood {name}"]
            path.unlink()
            return []
        if body.get("action") != "save":
            return ["action inválida (save|delete)"]
        desc = (body.get("description") or "").strip()
        text = (body.get("body") or "").strip()
        if not desc or not text:
            return ["faltan description y/o cuerpo"]
        enabled = "true" if body.get("enabled", True) else "false"
        content = (f"---\nname: {name}\ndescription: {desc}\nenabled: {enabled}\n---\n\n"
                   f"{text}\n")
        self._atomic_write(path, content)
        return []

    # Editor avanzado de prompts: solo archivos ya existentes en prompts/ de la
    # instancia (sin traversal: se valida por nombre pelado).
    def _prompt_files(self) -> list[str]:
        d = self.base / "prompts"
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir()
                      if p.is_file() and p.suffix in (".md", ".json"))

    def edit_prompt(self, body: dict) -> dict:
        fname = (body.get("file") or "").strip()
        if fname not in self._prompt_files():
            return {"ok": False, "errors": [f"archivo desconocido: {fname}"]}
        path = self.base / "prompts" / fname
        if body.get("action") == "get":
            return {"ok": True, "text": path.read_text(encoding="utf-8")}
        if body.get("action") == "save":
            text = body.get("text", "")
            if not text.strip():
                return {"ok": False, "errors": ["el prompt no puede quedar vacío"]}
            if fname.endswith(".json"):
                try:
                    json.loads(text)
                except json.JSONDecodeError as e:
                    return {"ok": False, "errors": [f"JSON inválido: {e}"]}
            self._atomic_write(path, text)
            return {"ok": True}
        return {"ok": False, "errors": ["action inválida (get|save)"]}

    def write_soul(self, text: str) -> list[str]:
        if not (text or "").strip():
            return ["SOUL.md no puede quedar vacío — es la personalidad del bot"]
        path = self.base / "context" / "SOUL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, text)
        return []

    def run_action(self, action: str) -> dict:
        """Corre un script de mantenimiento del motor sobre esta instancia."""
        src = Path(__file__).resolve().parent
        cmds = {
            "parse_memory": [sys.executable, str(src / "parse_memory.py"),
                             "--instance", str(self.base)],
            "catalog_sync": [sys.executable, str(src / "catalog.py"), "sync",
                             "--instance", str(self.base)],
        }
        if action not in cmds:
            return {"ok": False, "errors": [f"acción desconocida: {action}"]}
        try:
            r = subprocess.run(cmds[action], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600)
            out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
            return {"ok": r.returncode == 0, "output": out[-8000:],
                    "errors": [] if r.returncode == 0 else [f"exit {r.returncode}"]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "errors": ["timeout (10 min)"]}

    def debug_chat(self, text: str, author: str | None = None) -> dict:
        """Mensaje al pipeline REAL del bot (como mención del admin), sin red
        social: la respuesta vuelve acá en vez de postearse."""
        if not (text or "").strip():
            return {"ok": False, "errors": ["mensaje vacío"]}
        try:
            import botata
            reply = botata.debug_chat(text.strip(), author=author)
            return {"ok": True, "reply": reply}
        except SystemExit as e:
            return {"ok": False, "errors": [str(e)]}
        except Exception as e:
            return {"ok": False, "errors": [f"{type(e).__name__}: {e}"]}

    def danger(self, action: str, confirm: str) -> list[str]:
        """Acciones irreversibles. `confirm` debe ser el nombre de la instancia."""
        if confirm != self.base.name:
            return [f"confirmación incorrecta: escribí '{self.base.name}' para confirmar"]
        if action == "reset_memory":
            posted = self.base / "posted"
            for f in ("botata.db", "botata.db-wal", "botata.db-shm"):
                try:
                    (posted / f).unlink(missing_ok=True)
                except OSError as e:
                    return [f"no pude borrar {f}: {e} (¿el bot está corriendo?)"]
            return []
        if action == "delete_instance":
            repo_root = Path(__file__).resolve().parent.parent
            if self.base in (repo_root, repo_root.parent):
                return ["esta instancia es el repo mismo — borrala a mano si es lo que querés"]
            try:
                shutil.rmtree(self.base)
            except OSError as e:
                return [f"no pude borrar la instancia: {e} (¿el bot está corriendo?)"]
            return []
        return ["action inválida (reset_memory|delete_instance)"]

    def read_data(self) -> dict:
        """Datos vivos del DB del bot: memoria general, preferencias, calendario
        y el estado de mood. Best-effort: sin DB accesible devuelve vacío."""
        try:
            dbmod, conn = self._db()
        except Exception as e:
            return {"error": f"DB no accesible: {e}", "memory": [],
                    "preferences": [], "events": [], "mood_state": None}
        try:
            mood_state = None
            raw = dbmod.kv_get(conn, "mood_state")
            if raw:
                try:
                    mood_state = json.loads(raw)
                except json.JSONDecodeError:
                    pass
            events = conn.execute(
                "SELECT * FROM events "
                "WHERE datetime(replace(event_at,'T',' ')) >= datetime('now','-3 hours','-1 day') "
                "ORDER BY event_at ASC LIMIT 100").fetchall()
            return {
                "memory": dbmod.list_bot_memory(conn),
                "preferences": dbmod.list_preferences(conn),
                "events": [dict(r) for r in events],
                "mood_state": mood_state,
            }
        finally:
            conn.close()

    def edit_memory(self, body: dict) -> list[str]:
        dbmod, conn = self._db()
        try:
            if body.get("action") == "add":
                text = (body.get("text") or "").strip()
                if not text:
                    return ["falta el texto"]
                dbmod.add_bot_memory(conn, text, source="admin")
            elif body.get("action") == "delete":
                if not dbmod.delete_bot_memory(conn, int(body.get("id", 0))):
                    return ["id inexistente"]
            else:
                return ["action inválida (add|delete)"]
            return []
        finally:
            conn.close()

    def edit_preferences(self, body: dict) -> list[str]:
        dbmod, conn = self._db()
        try:
            if body.get("action") == "add":
                kind = (body.get("kind") or "").strip()
                text = (body.get("text") or "").strip()
                if kind not in _PREF_KINDS:
                    return ["kind inválido (like|dislike)"]
                if not text:
                    return ["falta el texto"]
                if dbmod.add_preference(conn, kind, text, source="admin") is None:
                    return ["ya existe una preferencia con ese texto"]
            elif body.get("action") == "delete":
                if not dbmod.delete_preference(conn, int(body.get("id", 0))):
                    return ["id inexistente"]
            else:
                return ["action inválida (add|delete)"]
            return []
        finally:
            conn.close()

    def edit_events(self, body: dict) -> list[str]:
        dbmod, conn = self._db()
        try:
            if body.get("action") == "add":
                title = (body.get("title") or "").strip()
                event_at = (body.get("event_at") or "").strip()
                errs = []
                if not title:
                    errs.append("falta title")
                if not _EVENT_AT_RE.match(event_at):
                    errs.append("event_at inválido (YYYY-MM-DD o YYYY-MM-DDTHH:MM)")
                if errs:
                    return errs
                handle = (body.get("handle") or "").strip().lstrip("@").lower() or None
                if handle and not dbmod.user_exists(conn, handle):
                    return [f"@{handle} no tiene perfil en el bot — dejá el handle "
                            "vacío para un evento de comunidad"]
                recur = (body.get("recur") or "").strip() or None
                if recur not in (None, "daily", "weekly", "monthly"):
                    return ["recur inválido (vacío|daily|weekly|monthly)"]
                dbmod.create_event(
                    conn, title=title, event_at=event_at, handle=handle,
                    description=(body.get("description") or "").strip() or None,
                    kind=(body.get("kind") or "other").strip() or "other",
                    source="ui", recur=recur)
            elif body.get("action") == "delete":
                if not dbmod.delete_event(conn, int(body.get("id", 0))):
                    return ["id inexistente"]
            else:
                return ["action inválida (add|delete)"]
            return []
        finally:
            conn.close()

    def _env_status(self) -> dict[str, bool]:
        """Qué claves están seteadas. NUNCA los valores."""
        present: set[str] = set()
        if self.env_path.exists():
            for line in self.env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key = line.split("=", 1)[0].strip()
                    if line.split("=", 1)[1].strip():
                        present.add(key)
        return {k: (k in present) for k in ENV_KEYS}

    def _skills_info(self) -> list[dict]:
        out = []
        if not self.skills_dir.is_dir():
            return out
        for path in sorted(self.skills_dir.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if parsed is None:
                continue
            meta, _ = parsed
            if not meta.get("name"):
                continue
            out.append({
                "name": meta["name"],
                "description": meta.get("description", ""),
                "scopes": meta.get("scopes", ""),
                "enabled": meta.get("enabled", "true").lower() != "false",
                "inline": meta.get("inline", "false").lower() == "true",
                "file": path.name,
            })
        return out

    # -- escritura -------------------------------------------------------------
    def write_settings(self, settings: dict) -> list[str]:
        errs = validate_settings(settings)
        if errs:
            return errs
        self._atomic_write(self.settings_path,
                           json.dumps(settings, ensure_ascii=False, indent="\t") + "\n")
        return []

    def write_news(self, news: list) -> list[str]:
        errs = validate_news(news)
        if errs:
            return errs
        self._atomic_write(self.news_path,
                           json.dumps(news, ensure_ascii=False, indent=2) + "\n")
        return []

    def update_env(self, updates: dict[str, str]) -> list[str]:
        """Actualiza SOLO las claves con valor no vacío; preserva el resto tal cual."""
        updates = {k: v for k, v in updates.items() if k in ENV_KEYS and (v or "").strip()}
        if not updates:
            return []
        lines = (self.env_path.read_text(encoding="utf-8").splitlines()
                 if self.env_path.exists() else [])
        seen: set[str] = set()
        out = []
        for line in lines:
            if "=" in line and not line.lstrip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    out.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            out.append(line)
        for key, value in updates.items():
            if key not in seen:
                out.append(f"{key}={value}")
        self._atomic_write(self.env_path, "\n".join(out) + "\n")
        return []

    def set_skill_enabled(self, name: str, enabled: bool) -> list[str]:
        """Reescribe solo la línea `enabled:` del frontmatter (o la inserta)."""
        for path in sorted(self.skills_dir.glob("*.md")):
            parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if parsed is None or parsed[0].get("name") != name:
                continue
            text = path.read_text(encoding="utf-8")
            value = "true" if enabled else "false"
            if re.search(r"(?m)^enabled\s*:", text):
                new = re.sub(r"(?m)^enabled\s*:.*$", f"enabled: {value}", text, count=1)
            else:  # insertar antes del cierre del frontmatter (segundo ---)
                new = re.sub(r"(?m)^---\s*$", f"enabled: {value}\n---", text.split("\n", 1)[1],
                             count=1)
                new = text.split("\n", 1)[0] + "\n" + new
            self._atomic_write(path, new)
            return []
        return [f"skill desconocida: {name}"]


# ─── HTTP ────────────────────────────────────────────────────────────────────
def make_handler(store: ConfigStore):

    class ConfigHandler(BaseHTTPRequestHandler):

        def log_message(self, fmt, *args):  # silenciar el log por request
            pass

        def _send(self, code: int, body: dict | bytes, ctype="application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, UI_HTML.read_bytes(), ctype="text/html")
            elif self.path == "/api/config":
                self._send(200, store.read_all())
            elif self.path == "/api/data":
                self._send(200, store.read_data())
            elif self.path == "/api/bot":
                self._send(200, bot_status(store.base))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                body = self._body()
                if self.path == "/api/settings":
                    errs = store.write_settings(body)
                elif self.path == "/api/news":
                    errs = store.write_news(body if isinstance(body, list) else body.get("news", []))
                elif self.path == "/api/env":
                    errs = store.update_env(body)
                elif self.path == "/api/skills":
                    errs = store.set_skill_enabled(body.get("name", ""), bool(body.get("enabled")))
                elif self.path == "/api/memory":
                    errs = store.edit_memory(body)
                elif self.path == "/api/preferences":
                    errs = store.edit_preferences(body)
                elif self.path == "/api/events":
                    errs = store.edit_events(body)
                elif self.path == "/api/soul":
                    errs = store.write_soul(body.get("text", ""))
                elif self.path == "/api/moods":
                    errs = store.edit_mood(body)
                elif self.path == "/api/prompt":
                    self._send(200, store.edit_prompt(body))
                    return
                elif self.path == "/api/bot":
                    errs = bot_control(store.base, body.get("action", ""),
                                       body.get("mode", "open"))
                elif self.path == "/api/run":
                    self._send(200, store.run_action(body.get("action", "")))
                    return
                elif self.path == "/api/debug/chat":
                    self._send(200, store.debug_chat(body.get("text", ""),
                                                     body.get("author")))
                    return
                elif self.path == "/api/danger":
                    errs = store.danger(body.get("action", ""), body.get("confirm", ""))
                else:
                    self._send(404, {"error": "not found"})
                    return
                if errs:
                    self._send(400, {"ok": False, "errors": errs})
                else:
                    self._send(200, {"ok": True})
            except json.JSONDecodeError:
                self._send(400, {"ok": False, "errors": ["JSON inválido"]})
            except Exception as e:  # nunca tirar el server por un request
                self._send(500, {"ok": False, "errors": [f"{type(e).__name__}: {e}"]})

    return ConfigHandler


def serve(base_dir: Path, port: int = 8787) -> ThreadingHTTPServer:
    """Crea el server (sin loop). El caller decide serve_forever/thread."""
    store = ConfigStore(base_dir)
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))


def main() -> None:
    parser = argparse.ArgumentParser(description="Panel de configuración de botata (localhost)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--instance", help="Directorio de la instancia a editar (default: raíz del repo)")
    args = parser.parse_args()
    httpd = serve(BASE_DIR, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"botata config UI → {url}  (Ctrl+C para salir)  [instancia: {BASE_DIR}]")
    print("Los cambios en settings/.env/noticias requieren reiniciar el bot.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nchau")


if __name__ == "__main__":
    main()
