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
import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
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
    "BSKY_PASSWORD", "MASTODON_ACCESS_TOKEN", "DISCORD_BOT_TOKEN",
    "OPENROUTER_API_KEY", "LLM_API_KEY",
    "BRAVE_API_KEY",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "YOUTUBE_API_KEY", "TUMBLR_API_KEY",
    "IG_USERNAME", "IG_PASSWORD", "GOOGLE_OAUTH_ID", "GOOGLE_OAUTH_SECRET",
]

# Nunca escribibles desde la UI, aunque una fuente las referencie: gobiernan el
# proceso, no una credencial de API.
_ENV_RESERVADAS = {"PATH", "PYTHONPATH", "BOTATA_INSTANCE", "HOME", "USERPROFILE",
                   "LD_PRELOAD", "PYTHONSTARTUP"}

_CHANNELS = {"bluesky", "mastodon", "discord"}
# `local` = timeline local (Mastodon); `channel` = mensajes de un canal (Discord)
_FEED_TYPES = {"list", "feed", "following", "local", "channel"}
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
    # LANGUAGE es texto libre ("es", "english", "portuñol rioplatense"...): se
    # inyecta tal cual en el identity_block del system prompt.
    if not (isinstance(s.get("LANGUAGE", "es"), str) and str(s.get("LANGUAGE", "es")).strip()):
        errs.append("LANGUAGE debe ser un texto no vacío (ej. 'es', 'english')")
    if not isinstance(s.get("BOT_NAME", ""), str):
        errs.append("BOT_NAME debe ser un string")
    if not isinstance(s.get("COMMUNITY_NAME", ""), str):
        errs.append("COMMUNITY_NAME debe ser un string")
    if s.get("BOT_ACTIONS_FROM", "admin") not in ("admin", "any"):
        errs.append("BOT_ACTIONS_FROM inválido (admin|any)")
    if not isinstance(s.get("READ_THREAD_MEDIA", True), bool):
        errs.append("READ_THREAD_MEDIA debe ser booleano")
    tzname = s.get("TIMEZONE", "")
    if tzname:
        if not isinstance(tzname, str):
            errs.append("TIMEZONE debe ser un string")
        elif not re.fullmatch(r"UTC[+-]\d{1,2}(:\d{2})?", tzname.strip(), re.I):
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tzname.strip())
            except Exception:
                errs.append(f"TIMEZONE '{tzname}' no es una zona IANA válida "
                            "(ej. America/Argentina/Buenos_Aires) ni un offset 'UTC-3'")
    if channel == "mastodon" and not str(
            s.get("MASTODON_BASE_URL", "")).startswith(("http://", "https://")):
        errs.append("CHANNEL=mastodon requiere MASTODON_BASE_URL "
                    "(ej. https://mastodon.social)")
    if channel == "discord":
        ids = s.get("DISCORD_CHANNEL_IDS", [])
        if (not isinstance(ids, list) or not ids
                or not all(str(i).strip().isdigit() for i in ids)):
            errs.append("CHANNEL=discord requiere DISCORD_CHANNEL_IDS: lista no "
                        "vacía de ids numéricos de canal (el primero es el principal)")
    if not isinstance(s.get("POLL_INTERVAL_SECONDS", 60), (int, float)):
        errs.append("POLL_INTERVAL_SECONDS debe ser numérico")

    conns = s.get("CONNECTORS", {})
    if not isinstance(conns, dict):
        errs.append("CONNECTORS debe ser un objeto {conector: {enabled: bool}}")
    else:
        for cid, cfg in conns.items():
            if cid not in SOURCE_TYPES:
                errs.append(f"CONNECTORS.{cid}: conector desconocido (usar {list(SOURCE_TYPES)})")
            elif not isinstance((cfg or {}).get("enabled", True), bool):
                errs.append(f"CONNECTORS.{cid}.enabled debe ser booleano")

    for i, feed in enumerate(s.get("FEEDS", [])):
        tag = f"FEEDS[{i}]"
        if not feed.get("name"):
            errs.append(f"{tag}: falta name")
        if feed.get("type", "list") not in _FEED_TYPES:
            errs.append(f"{tag}: type inválido '{feed.get('type')}' (usar {sorted(_FEED_TYPES)})")
        if feed.get("type", "list") not in ("following", "local") and not feed.get("uri"):
            errs.append(f"{tag}: falta uri (obligatoria salvo type=following/local)")

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

    ca = s.get("CALENDAR_ANNOUNCE", {})
    if not isinstance(ca, dict):
        errs.append("CALENDAR_ANNOUNCE debe ser un objeto {from, groups}")
    else:
        if str(ca.get("from", "admin")) not in ("admin", "groups", "any"):
            errs.append("CALENDAR_ANNOUNCE.from inválido (admin|groups|any)")
        if not isinstance(ca.get("feed", False), bool):
            errs.append("CALENDAR_ANNOUNCE.feed debe ser booleano")
        ca_groups = ca.get("groups", [])
        if not (isinstance(ca_groups, list)
                and all(isinstance(g, str) and g.strip() for g in ca_groups)):
            errs.append("CALENDAR_ANNOUNCE.groups debe ser una lista de nombres de grupo")
        elif str(ca.get("from", "admin")) == "groups":
            unknown = set(ca_groups) - set(groups)
            if unknown:
                errs.append(f"CALENDAR_ANNOUNCE.groups: grupo(s) desconocido(s) "
                            f"{sorted(unknown)} (definilos en USER_GROUPS)")

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


import connectors as connectorsmod

SOURCE_TYPES = connectorsmod.all_ids()


def validate_sources(sources: list) -> list[str]:
    """T38b: registro ÚNICO de fuentes de contenido (config/sources.json).

    [{type, name, category, sources: [...], description, enabled}] — un tema con
    las fuentes que le correspondan. RSS es un `type` más, no una sección aparte.
    """
    errs = []
    for i, entry in enumerate(sources or []):
        tag = f"sources[{i}]"
        kind = (entry.get("type") or "").strip().lower()
        if kind == "scrape":                       # nombre viejo del tipo
            kind = entry["type"] = "membrilla"
        if kind not in SOURCE_TYPES:
            errs.append(f"{tag}: type inválido '{entry.get('type')}' (usar {list(SOURCE_TYPES)})")
        srcs = entry.get("sources")
        if isinstance(srcs, str):
            srcs = srcs.split(",")
        srcs = [str(s).strip() for s in (srcs or []) if str(s).strip()]
        if not srcs:
            errs.append(f"{tag}: sin fuentes")
        if kind == "rss":
            for u in srcs:
                if not u.startswith(("http://", "https://")):
                    errs.append(f"{tag}: '{u}' no es una URL de feed válida")
        if kind == "pinterest":
            for s in srcs:
                if not s.startswith("http") and s.count("/") != 1:
                    errs.append(f"{tag}: '{s}' tiene que ser 'usuario/tablero' o la URL del tablero")
        if kind == "api":
            # El conector declarativo: sin URL ni campo de imagen no hay nada que
            # traer. Las credenciales que falten NO son error de validación — se
            # pueden cargar después en el .env; el botón de probar las avisa.
            url = str(entry.get("url") or "").strip()
            if not url:
                errs.append(f"{tag}: falta la URL de la API")
            elif not url.startswith(("http://", "https://")):
                errs.append(f"{tag}: la URL tiene que empezar con http:// o https://")
            if not str((entry.get("map") or {}).get("image_url") or "").strip():
                errs.append(f"{tag}: falta indicar de qué campo de la respuesta sale la imagen")
        if not (entry.get("category") or "").strip() and not (entry.get("name") or "").strip():
            errs.append(f"{tag}: poné al menos un tema (category) o un nombre")
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


def _matar_arbol(proc: subprocess.Popen) -> None:
    """Mata el proceso del bot Y sus hijos.

    `terminate()` solo mataba al proceso lanzado; con pyenv-win (y con cualquier
    launcher que haga de shim) el intérprete real es un HIJO y sobrevivía. Así
    quedó un bot huérfano polleando en paralelo al nuevo — dos bots sobre la
    misma instancia, pisándose las menciones. En Windows se usa taskkill /T.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception as e:
        log.warning("no pude matar el árbol del bot (%s): %s", proc.pid, e)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _bomba_de_log(proc: subprocess.Popen, log_path: Path) -> None:
    """Vuelca la salida del bot al archivo y a la consola, línea por línea."""
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            for linea in proc.stdout:            # type: ignore[union-attr]
                f.write(linea)
                f.flush()
                sys.stdout.write(linea)
                sys.stdout.flush()
    except Exception as e:                        # el bot no depende de su propio log
        log.debug("bomba de log cortada: %s", e)


def bot_status(base_dir: Path, lineas: int = 200) -> dict:
    proc = _BOT.get("proc")
    running = proc is not None and proc.poll() is None
    log_path = base_dir / "bot.log"
    tail = ""
    if log_path.exists():
        try:
            tail = "\n".join(
                log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lineas:])
        except Exception:
            pass
    return {"running": running, "pid": proc.pid if running else None, "log_tail": tail}


def bot_control(base_dir: Path, action: str, mode: str = "open") -> list[str]:
    proc = _BOT.get("proc")
    if action in ("stop", "restart") and proc is not None and proc.poll() is None:
        _matar_arbol(proc)
        if proc.stdout is not None:
            try:
                proc.stdout.close()      # corta la bomba de log
            except Exception:
                pass
        _BOT["proc"] = None
    if action in ("start", "restart"):
        if _BOT.get("proc") is not None and _BOT["proc"].poll() is None:
            return ["el bot ya está corriendo (pará primero)"]
        if mode not in ("open", "admin_only"):
            return ["mode inválido (open|admin_only)"]
        # `-u`: sin esto Python bufferea stdout al no ser una terminal (bloques de
        # 8 KB), así que las primeras líneas del arranque quedaban retenidas y el
        # log parecía vacío justo cuando uno lo mira.
        _BOT["proc"] = subprocess.Popen(
            [sys.executable, "-u", "-m", "botata",
             "--instance", str(base_dir), "--mode", mode],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            # grupo propio: permite matar el árbol entero en POSIX (killpg)
            start_new_session=(os.name != "nt"))
        # Se lee en un hilo y se escribe a los DOS lados: al archivo (lo que lee el
        # panel de la UI) y a la consola donde corre este panel, que es donde uno
        # naturalmente mira cuando arranca el bot desde acá.
        threading.Thread(target=_bomba_de_log,
                         args=(_BOT["proc"], base_dir / "bot.log"),
                         daemon=True, name="bot-log").start()
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
        self.news_path = self.base / "config" / "news_sites.json"          # legado
        self.content_sources_path = self.base / "config" / "content_sources.json"  # legado
        self.sources_path = self.base / "config" / "sources.json"
        self.env_path = self.base / ".env"
        self.skills_dir = self.base / "skills"
        self.moods_dir = self.base / "moods"
        self.db_path = self.base / "posted" / "botata.db"

    def _db(self):
        """Conexión al DB del bot (WAL: convive con el bot corriendo). Lazy
        import para que la UI arranque aunque falte sqlite-vec."""
        import db as dbmod
        try:  # los defaults de "hoy"/"ahora" del calendario usan la tz de la instancia
            tz = json.loads(self.settings_path.read_text(encoding="utf-8")).get("TIMEZONE", "")
            if tz:
                dbmod.set_local_tz(tz)
        except Exception:
            pass
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
        connectorsmod.load_instance_plugins(self.base / "connectors")
        sources = self._read_sources(settings)
        soul_path = self.base / "context" / "SOUL.md"
        scrape = self.base / "scrape"
        try:  # indicador de Membrilla: cuántos sidecars dejó en la carpeta
            scrape_items = sum(1 for _ in scrape.rglob("*.json")) if scrape.is_dir() else 0
        except OSError:
            scrape_items = 0
        # Cuánto de eso ya está indexado en el catálogo (best-effort, DB readonly).
        catalog_items = 0
        catalog_sources: list[str] = []
        db_path = self.base / "posted" / "botata.db"
        if db_path.exists():
            try:
                import sqlite3
                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                    catalog_items = conn.execute(
                        "SELECT COUNT(*) FROM image_catalog").fetchone()[0]
                    # Fuentes ya indexadas: el admin necesita el nombre EXACTO para
                    # registrarlas por tema (T38) — la UI las ofrece para copiar.
                    catalog_sources = [r[0] for r in conn.execute(
                        "SELECT DISTINCT source_name FROM image_catalog "
                        "WHERE source_name <> '' ORDER BY source_name").fetchall()]
            except Exception:
                pass
        return {
            "settings": settings,
            "sources": sources,
            "catalog_sources": catalog_sources,
            # Los conectores propios de la instancia (connectors/*.py) también son
            # parte del catálogo: sin esto la UI mostraba solo los built-in y un
            # plugin recién puesto era invisible.
            "connectors": connectorsmod.catalog(settings),
            "env_keys": self._env_status(),
            "skills": self._skills_info(),
            "routines": self._routines_info(),
            "moods": self._moods_info(),
            "prompts": self._prompt_files(),
            "soul": soul_path.read_text(encoding="utf-8") if soul_path.exists() else "",
            "tools_catalog": tool_catalog(settings),
            "instance": {"path": str(self.base), "name": self.base.name,
                         "scrape_dir": str(scrape), "scrape_items": scrape_items,
                         "catalog_items": catalog_items},
        }

    def _read_sources(self, settings: dict) -> list[dict]:
        """Registro único. Si todavía no existe, migra en memoria los archivos
        viejos (news_sites.json → rss, content_sources.json → scrape) + la
        playlist suelta de settings, para que la UI muestre todo desde el día 1."""
        if self.sources_path.exists():
            try:
                return json.loads(self.sources_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        out: list[dict] = []
        try:
            for e in json.loads(self.news_path.read_text(encoding="utf-8")):
                if isinstance(e, str):
                    e = {"url": e}
                if (e.get("url") or "").strip():
                    out.append({"type": "rss", "name": e.get("title", ""),
                                "category": e.get("category", ""), "sources": [e["url"]],
                                "description": e.get("description", ""),
                                "enabled": e.get("enabled", True)})
        except Exception:
            pass
        try:
            for e in json.loads(self.content_sources_path.read_text(encoding="utf-8")):
                if isinstance(e, dict):
                    srcs = e.get("sources") or ([e["source"]] if e.get("source") else [])
                    if srcs:
                        out.append({**e, "type": "membrilla", "sources": srcs})
        except Exception:
            pass
        pl = str(settings.get("SPOTIFY_PLAYLIST_ID", "")).strip()
        if pl:
            out.append({"type": "spotify", "name": "playlist comunitaria",
                        "category": "", "sources": [pl], "description": "", "enabled": True})
        return out

    def _routines_info(self) -> list[dict]:
        """Rutinas (conducta proactiva, routines/*.md) con cuerpo — para la UI."""
        out = []
        routines_dir = self.base / "routines"
        if not routines_dir.is_dir():
            return out
        for path in sorted(routines_dir.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if parsed is None:
                continue
            meta, body = parsed
            out.append({
                "file": path.name, "name": path.stem,
                "channel": (meta.get("channel") or "").strip(),
                "interval_hours": meta.get("interval_hours", "0"),
                "enabled": meta.get("enabled", "true").lower() != "false",
                "body": body.strip(),
            })
        return out

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
                "triggers": meta.get("triggers", "").strip(),
                "enabled": meta.get("enabled", "true").lower() != "false",
                "body": body.strip(),
                "file": path.name,
            })
        return out

    _MOOD_NAME_RE = re.compile(r"^[a-z0-9_-]{2,30}$")

    def set_mood_today(self, body: dict) -> list[str]:
        """Fija el mood de HOY en vivo (kv `mood_state`, lo que lee current_mood
        en modo auto). mood vacío = soltar (vuelve al default/decisión del bot).
        Ojo: en MOODS.mode manual manda `manual.fixed/schedule` (settings)."""
        name = (body.get("mood") or "").strip().lower()
        if name and not (self.moods_dir / f"{name}.md").exists():
            return [f"no existe el mood '{name}'"]
        dbmod, conn = self._db()
        try:
            if not name:
                conn.execute("DELETE FROM kv WHERE key = 'mood_state'")
                conn.commit()
                return []
            from datetime import datetime, timedelta, timezone
            today = (datetime.now(timezone.utc) - timedelta(hours=3)).date().isoformat()
            dbmod.kv_set(conn, "mood_state", json.dumps(
                {"date": today, "mood": name, "mode": "ui",
                 "reason": "seteado por el admin desde la UI"}))
            return []
        finally:
            conn.close()

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
        # triggers (opcional): qué pone al bot en este estado — el selector auto
        # de mood los prioriza contra el clima leído. Vacío = línea ausente.
        triggers = (body.get("triggers") or "").strip().replace("\n", " ")
        enabled = "true" if body.get("enabled", True) else "false"
        content = (f"---\nname: {name}\ndescription: {desc}\n"
                   + (f"triggers: {triggers}\n" if triggers else "")
                   + f"enabled: {enabled}\n---\n\n{text}\n")
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
        if action == "membrilla_scrape":
            return self._run_membrilla()
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

    def _run_membrilla(self) -> dict:
        """Lanza el scraper Membrilla (suite hermana) según settings.MEMBRILLA:
        {"repo": ruta del repo membrilla, "commands": ["python scrape_ig.py api-run", ...]}.
        Cada comando corre con cwd=repo y MEMBRILLA_OUTPUT_DIR apuntando al scrape/
        de ESTA instancia, así el crudo cae donde el indexador lo espera."""
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "errors": [f"no pude leer settings.json: {e}"]}
        cfg = settings.get("MEMBRILLA") or {}
        repo = Path(str(cfg.get("repo") or "")).expanduser()
        commands = [c for c in (cfg.get("commands") or []) if str(c).strip()]
        if not cfg.get("repo") or not commands:
            return {"ok": False, "errors": [
                "MEMBRILLA sin configurar: en settings.json poné "
                '{"MEMBRILLA": {"repo": "ruta al repo membrilla", '
                '"commands": ["python scrape_ig.py api-run"]}}']}
        if not repo.is_dir():
            return {"ok": False, "errors": [f"repo de Membrilla no existe: {repo}"]}
        env = {**os.environ, "MEMBRILLA_OUTPUT_DIR": str(self.base / "scrape")}
        chunks, ok = [], True
        for cmd in commands:
            chunks.append(f"$ {cmd}")
            try:
                r = subprocess.run(str(cmd), shell=True, cwd=str(repo), env=env,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=1800)
                chunks.append(((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip())
                if r.returncode != 0:
                    ok = False
                    chunks.append(f"✗ exit {r.returncode}")
            except subprocess.TimeoutExpired:
                ok = False
                chunks.append("✗ timeout (30 min) — comando cortado")
        return {"ok": ok, "output": "\n\n".join(chunks)[-8000:],
                "errors": [] if ok else ["algún comando falló — mirá el output"]}

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
            # corte "desde ayer" calculado en Python con la tz de la instancia
            # (LOCAL_TZ, seteada en _db) — nada de offsets hardcodeados en SQL
            cutoff = (dbmod.local_now() - timedelta(days=1)).isoformat(timespec="minutes")
            events = conn.execute(
                "SELECT * FROM events "
                "WHERE datetime(replace(event_at,'T',' ')) >= datetime(replace(?,'T',' ')) "
                "ORDER BY event_at ASC LIMIT 100", (cutoff,)).fetchall()
            ev_list = []
            for r in events:
                ev = dict(r)
                # announce efectivo para mostrar el switch: NULL (legado/groups)
                # → se estima con la política actual (igual que el gate del motor,
                # sin membresías por feed — best-effort para la UI).
                ev["announce_effective"] = (bool(ev["announce"])
                                            if ev.get("announce") is not None
                                            else self._announce_default(ev.get("source")))
                ev_list.append(ev)
            rt_rows = conn.execute(
                "SELECT feed_name, last_run FROM feed_cursors "
                "WHERE feed_name LIKE 'routine:%'").fetchall()
            return {
                "memory": dbmod.list_bot_memory(conn),
                "preferences": dbmod.list_preferences(conn),
                "events": ev_list,
                "mood_state": mood_state,
                "routine_last_runs": {r["feed_name"][len("routine:"):]: r["last_run"]
                                      for r in rt_rows},
                "bio_current": dbmod.kv_get(conn, "bio_current"),
            }
        finally:
            conn.close()

    def _announce_default(self, source: str | None) -> bool:
        """Estimación UI del switch de anuncio para eventos legados (announce
        NULL): misma política que el motor (CALENDAR_ANNOUNCE + creador), sin
        resolver membresías por feed (best-effort de solo lectura)."""
        try:
            s = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        src = (source or "").strip()
        ca = s.get("CALENDAR_ANNOUNCE", {}) or {}
        if src == "feed":
            return bool(ca.get("feed", False))
        admins = {str(h).lstrip("@").lower()
                  for h in [s.get("ADMIN_HANDLE", "")] + list(s.get("ADMIN_HANDLES") or [])
                  if h}
        creator = None
        if src.startswith("tool:@"):
            creator = src[len("tool:@"):].lower() or None
        elif src in ("ui", "admin") or src.startswith("/"):
            return True
        if creator in admins:
            return True
        mode = str(ca.get("from", "admin")).lower()
        if mode == "any":
            return True
        if mode == "groups" and creator:
            allowed = set(ca.get("groups") or [])
            groups = s.get("USER_GROUPS", {}) or {}
            member = {g for g, hs in groups.items()
                      if isinstance(hs, list)
                      and creator in {str(h).lstrip("@").lower() for h in hs}}
            return bool(allowed & member)
        return False

    def set_event_announce(self, body: dict) -> list[str]:
        """Switch 📣 por evento: prende/apaga el anuncio automático (tarea
        calendar) de un evento puntual. Deja el valor EXPLÍCITO en DB."""
        dbmod, conn = self._db()
        try:
            eid = int(body.get("id", 0))
            if not dbmod.get_event(conn, eid):
                return ["id de evento inexistente"]
            dbmod.set_event_announce(conn, eid, bool(body.get("announce")))
            return []
        finally:
            conn.close()

    def edit_memory(self, body: dict) -> list[str]:
        dbmod, conn = self._db()
        try:
            if body.get("action") == "add":
                text = (body.get("text") or "").strip()
                if not text:
                    return ["falta el texto"]
                # Lo que carga el admin a mano nace 📌: lo escribió una persona
                # con intención, no lo dedujo el bot de una conversación.
                dbmod.add_bot_memory(conn, text, source="admin", pinned=True)
            elif body.get("action") == "delete":
                if not dbmod.delete_bot_memory(conn, int(body.get("id", 0))):
                    return ["id inexistente"]
            elif body.get("action") == "pin":
                if not dbmod.set_bot_memory_pinned(
                        conn, int(body.get("id", 0)), bool(body.get("pinned"))):
                    return ["id inexistente"]
            else:
                return ["action inválida (add|delete|pin)"]
            return []
        finally:
            conn.close()

    def read_user_memory(self, handle: str | None = None) -> dict:
        """Visor de memoria por usuario. Sin handle → overview (usuarios con
        conteo de hechos + lecciones); con handle → sus hechos e interacciones.
        Best-effort: sin DB accesible devuelve vacío con error legible."""
        try:
            dbmod, conn = self._db()
        except Exception as e:
            return {"error": f"DB no accesible: {e}", "users": [], "lessons": []}
        try:
            if not handle:
                users = conn.execute(
                    "SELECT u.handle, u.display_name, COUNT(f.id) AS facts "
                    "FROM users u LEFT JOIN user_facts f ON f.handle = u.handle "
                    "GROUP BY u.handle ORDER BY facts DESC, u.handle").fetchall()
                lessons = conn.execute(
                    "SELECT id, scope, lesson_text, created_at FROM lessons "
                    "ORDER BY id DESC LIMIT 200").fetchall()
                return {"users": [dict(r) for r in users],
                        "lessons": [dict(r) for r in lessons]}
            handle = handle.strip().lstrip("@").lower()
            facts = conn.execute(
                "SELECT id, fact_text, source_uri, created_at FROM user_facts "
                "WHERE handle = ? ORDER BY id DESC", (handle,)).fetchall()
            inter = conn.execute(
                "SELECT id, summary, source_uri, created_at FROM interactions "
                "WHERE handle = ? ORDER BY id DESC LIMIT 50", (handle,)).fetchall()
            return {"facts": [dict(r) for r in facts],
                    "interactions": [dict(r) for r in inter]}
        finally:
            conn.close()

    _USERMEM_KINDS = {"fact": ("user_facts", "user_facts_vec"),
                      "lesson": ("lessons", "lessons_vec"),
                      "interaction": ("interactions", None)}

    def delete_user_memory(self, body: dict) -> list[str]:
        """Borra un registro de memoria semántica. fact/lesson limpian también su
        embedding en vec0 (misma lógica que mem_admin — sin esto quedan vectores
        huérfanos); el índice FTS se sincroniza solo por triggers."""
        kind = body.get("kind")
        if kind not in self._USERMEM_KINDS:
            return ["kind inválido (fact|lesson|interaction)"]
        try:
            rid = int(body.get("id", 0))
        except (TypeError, ValueError):
            return ["id inválido"]
        table, vec = self._USERMEM_KINDS[kind]
        dbmod, conn = self._db()
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (rid,))
            if cur.rowcount == 0:
                return ["id inexistente"]
            if vec:
                conn.execute(f"DELETE FROM {vec} WHERE rowid = ?", (rid,))
            conn.commit()
            return []
        finally:
            conn.close()

    def add_user_memory(self, body: dict) -> list[str]:
        """Alta manual de memoria semántica: fact (por usuario) o lesson
        (conductual, scope community o user:<handle>). Usa los MISMOS upserts
        que el bot — dedup semántico incluido. Ojo: la primera alta carga el
        modelo de embeddings (bge-m3) en el proceso de la UI (~30s, una vez)."""
        kind = body.get("kind")
        text = (body.get("text") or "").strip()
        if kind not in ("fact", "lesson"):
            return ["kind inválido (fact|lesson)"]
        if not text:
            return ["falta el texto"]
        dbmod, conn = self._db()
        try:
            if kind == "fact":
                handle = (body.get("handle") or "").strip().lstrip("@").lower()
                if not handle:
                    return ["falta el handle"]
                if not dbmod.user_exists(conn, handle):
                    return [f"@{handle} no tiene perfil en la DB todavía "
                            "(el bot crea perfiles al interactuar)"]
                rid = dbmod.upsert_user_fact(conn, handle, text, source_uri="ui")
            else:
                scope = (body.get("scope") or "community").strip() or "community"
                if scope != "community" and not scope.startswith("user:"):
                    return ["scope inválido (community | user:<handle>)"]
                rid = dbmod.upsert_lesson(conn, text, scope)
            if rid is None:
                return ["ya existe una memoria semánticamente equivalente (dedup)"]
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
                if recur not in (None, "daily", "weekly", "monthly", "yearly"):
                    return ["recur inválido (vacío|daily|weekly|monthly|yearly)"]
                dbmod.create_event(
                    conn, title=title, event_at=event_at, handle=handle,
                    description=(body.get("description") or "").strip() or None,
                    kind=(body.get("kind") or "other").strip() or "other",
                    source="ui", recur=recur, announce=True)
            elif body.get("action") == "edit":
                # Edición acotada a fecha/hora y repetición (lo que pide cambiar
                # un evento mal agendado; título/dueño = borrar y recrear).
                event_at = (body.get("event_at") or "").strip()
                if not _EVENT_AT_RE.match(event_at):
                    return ["event_at inválido (YYYY-MM-DD o YYYY-MM-DDTHH:MM)"]
                recur = (body.get("recur") or "").strip() or None
                if recur not in (None, "daily", "weekly", "monthly", "yearly"):
                    return ["recur inválido (vacío|daily|weekly|monthly|yearly)"]
                if not dbmod.update_event(conn, int(body.get("id", 0)),
                                          event_at=event_at, recur=recur):
                    return ["id inexistente"]
            elif body.get("action") == "delete":
                if not dbmod.delete_event(conn, int(body.get("id", 0))):
                    return ["id inexistente"]
            else:
                return ["action inválida (add|edit|delete)"]
            return []
        finally:
            conn.close()

    # ─── Importación de calendario (CSV / ICS) ──────────────────────────────
    _IMPORT_KINDS = {"other", "birthday", "meetup", "reminder", "community"}

    def import_events(self, body: dict) -> dict:
        """Importa eventos desde texto CSV o ICS (autodetectado por contenido).
        dry_run=True (default) → solo vista previa por fila, no escribe nada;
        False → inserta las filas OK (source='import'). `bot_action` queda
        excluido a propósito: las órdenes al bot se agendan a mano."""
        text = (body.get("text") or "").strip().lstrip("﻿")
        if not text:
            return {"ok": False, "errors": ["texto vacío: pegá el CSV o el ICS"]}
        dry = bool(body.get("dry_run", True))
        try:
            rows = _parse_ics(text) if text.startswith("BEGIN:VCALENDAR") \
                else _parse_import_csv(text)
        except ValueError as e:
            return {"ok": False, "errors": [str(e)]}
        if not rows:
            return {"ok": False, "errors": ["no encontré ningún evento en el texto"]}
        dbmod, conn = self._db()
        try:
            results, importados = [], 0
            for r in rows:
                item = {"fila": r["fila"], "titulo": r["titulo"], "estado": "ok",
                        "detalle": ""}
                err = r["error"] or self._validate_import_row(conn, dbmod, r)
                if err:
                    item.update(estado="error", detalle=err)
                elif dbmod.event_exists(conn, title=r["titulo"],
                                        event_at=r["fecha"], handle=r["handle"]):
                    item.update(estado="duplicado", detalle="ya estaba agendado")
                else:
                    item["detalle"] = r["fecha"] \
                        + (f" · {r['recur']}" if r["recur"] else "") \
                        + (f" · @{r['handle']}" if r["handle"] else " · comunidad")
                    if not dry:
                        # announce=True: el import lo hace el admin desde la UI
                        # (source 'import' era origen desconocido para el gate
                        # viejo y los eventos importados no se anunciaban nunca).
                        dbmod.create_event(
                            conn, title=r["titulo"], event_at=r["fecha"],
                            handle=r["handle"], description=r["descripcion"] or None,
                            kind=r["tipo"], source="import", recur=r["recur"],
                            announce=True)
                        importados += 1
                results.append(item)
            return {"ok": True, "dry_run": dry, "rows": results, "importados": importados,
                    "importables": sum(1 for x in results if x["estado"] == "ok")}
        finally:
            conn.close()

    def _validate_import_row(self, conn, dbmod, r: dict) -> str | None:
        if not r["titulo"]:
            return "falta el título"
        if not _EVENT_AT_RE.match(r["fecha"] or ""):
            return "fecha inválida (YYYY-MM-DD o YYYY-MM-DDTHH:MM)"
        if r["tipo"] == "bot_action":
            return "bot_action no se importa (las órdenes al bot van a mano)"
        if r["tipo"] not in self._IMPORT_KINDS:
            return f"tipo desconocido: '{r['tipo']}' (usar {'|'.join(sorted(self._IMPORT_KINDS))})"
        if r["handle"] and not dbmod.user_exists(conn, r["handle"]):
            return f"@{r['handle']} no tiene perfil en el bot (dejá de_quien vacío = comunidad)"
        return None

    def _env_status(self) -> dict[str, bool]:
        """Qué claves están seteadas. NUNCA los valores."""
        present: set[str] = set()
        if self.env_path.exists():
            for line in self.env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key = line.split("=", 1)[0].strip()
                    if line.split("=", 1)[1].strip():
                        present.add(key)
        return {k: (k in present) for k in [*ENV_KEYS, *self._env_keys_de_fuentes()]}

    def _env_keys_de_fuentes(self) -> list[str]:
        """Credenciales que pidió el admin al describir una fuente `api`.

        La whitelist fija no puede conocer de antemano el nombre de la clave de
        una API que todavía no existe, y sin esto el conector declarativo no
        serviría para ninguna API con token: habría que editar el `.env` a mano,
        que es justo lo que este conector viene a evitar. El permiso queda
        acotado: solo se pueden escribir claves **referenciadas desde
        sources.json**, nunca un nombre arbitrario venido del navegador."""
        out: list[str] = []
        try:
            entries = json.loads(self.sources_path.read_text(encoding="utf-8"))
        except Exception:
            return out
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and (e.get("type") or "").strip().lower() == "api":
                out += [v for v in connectorsmod.entry_env_vars(e)
                        if v not in ENV_KEYS and v not in _ENV_RESERVADAS and v not in out]
        return out

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

    def write_sources(self, sources: list) -> list[str]:
        errs = validate_sources(sources)
        if errs:
            return errs
        norm = []
        for e in sources:
            srcs = e.get("sources")
            if isinstance(srcs, str):
                srcs = srcs.split(",")
            norm.append({**e, "sources": [str(s).strip() for s in (srcs or []) if str(s).strip()]})
        self._atomic_write(self.sources_path,
                           json.dumps(norm, ensure_ascii=False, indent=2) + "\n")
        # Los registros viejos ya no gobiernan nada: se retiran para no confundir.
        for legacy in (self.news_path, self.content_sources_path):
            if legacy.exists():
                legacy.replace(legacy.with_suffix(legacy.suffix + ".migrado"))
        return []

    def test_source(self, body: dict) -> dict:
        """Corre UNA fuente ahora y cuenta qué trajo, sin guardar nada.

        Es la pieza que hace usable al conector declarativo para alguien que no
        programa: sin esto, una URL mal escrita o un campo mal elegido se ven
        igual que una API caída — el bot simplemente no postea y no hay dónde
        mirar. Acá el error vuelve explicado."""
        entry = dict(body.get("entry") or {})
        kind = (entry.get("type") or "").strip().lower()
        srcs = entry.get("sources")
        if isinstance(srcs, str):
            srcs = srcs.split(",")
        srcs = [str(s).strip() for s in (srcs or []) if str(s).strip()]
        entry["sources"] = srcs
        source = str(body.get("source") or (srcs[0] if srcs else "")).strip()
        if not source:
            return {"ok": False, "error": "escribí al menos una fuente para probar"}
        faltan = [v for v in connectorsmod.entry_env_vars(entry) if not os.environ.get(v)]
        if kind == "api":
            try:
                items = connectorsmod.api_items(source, 5, entry)
            except connectorsmod.ApiSourceError as e:
                return {"ok": False, "error": str(e), "missing_env": faltan, "source": source}
            except Exception as e:                     # bug nuestro, no del admin
                log.exception("test_source")
                return {"ok": False, "error": f"error inesperado: {e}", "source": source}
        elif connectorsmod.fetcher(kind):
            items = connectorsmod.fetch_items(kind, source, limit=5, entry=entry)
            if not items:
                return {"ok": False, "source": source,
                        "error": "no trajo nada — mirá el log del bot para el detalle"}
        else:
            return {"ok": False, "source": source,
                    "error": f"el conector '{kind}' solo se puede probar con el bot corriendo"}
        return {"ok": True, "count": len(items), "sample": items[0], "source": source}

    def update_env(self, updates: dict[str, str]) -> list[str]:
        """Actualiza SOLO las claves con valor no vacío; preserva el resto tal cual."""
        permitidas = {*ENV_KEYS, *self._env_keys_de_fuentes()}
        updates = {k: v for k, v in updates.items() if k in permitidas and (v or "").strip()}
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

    # Editor de archivos de comportamiento (skills/*.md y routines/*.md): la UI
    # puede crearlos/editarlos/borrarlos enteros. Nombre pelado (sin traversal);
    # el save valida el frontmatter mínimo de cada tipo.
    _BEHAVIOR_DIRS = {"skill": "skills", "routine": "routines"}
    _BEHAVIOR_FILE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}\.md$")

    def edit_behavior_file(self, body: dict) -> dict:
        kind = body.get("kind")
        subdir = self._BEHAVIOR_DIRS.get(kind)
        if not subdir:
            return {"ok": False, "errors": ["kind inválido (skill|routine)"]}
        fname = (body.get("file") or "").strip()
        if not self._BEHAVIOR_FILE_RE.match(fname) or fname.upper() == "README.MD":
            return {"ok": False, "errors":
                    ["nombre inválido (letras/números/guiones + .md, no README)"]}
        path = self.base / subdir / fname
        action = body.get("action")
        if action == "get":
            if not path.exists():
                return {"ok": False, "errors": [f"{subdir}/{fname} no existe"]}
            return {"ok": True, "text": path.read_text(encoding="utf-8")}
        if action == "delete":
            if not path.exists():
                return {"ok": False, "errors": [f"{subdir}/{fname} no existe"]}
            path.unlink()
            return {"ok": True}
        if action != "save":
            return {"ok": False, "errors": ["action inválida (get|save|delete)"]}
        text = body.get("text", "")
        parsed = _parse_frontmatter(text)
        if parsed is None:
            return {"ok": False, "errors": ["falta el frontmatter (bloque --- ... ---)"]}
        meta, fbody = parsed
        if kind == "skill" and not (meta.get("name") and meta.get("description")):
            return {"ok": False, "errors":
                    ["una skill necesita name y description en el frontmatter"]}
        if kind == "routine":
            channel = (meta.get("channel") or "").strip()
            if channel and not channel.isdigit():
                return {"ok": False, "errors":
                        ["channel debe ser el id numérico de un canal de Discord "
                         "(u omitirse para postear al feed principal)"]}
            try:
                float(meta.get("interval_hours", "0") or 0)
            except ValueError:
                return {"ok": False, "errors": ["interval_hours debe ser numérico"]}
        if not fbody.strip():
            return {"ok": False, "errors": ["el cuerpo no puede quedar vacío"]}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, text if text.endswith("\n") else text + "\n")
        return {"ok": True}

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


# ─── Parsers del import de calendario ────────────────────────────────────────
# CSV: encabezado fecha,titulo[,descripcion,tipo,de_quien,repeticion] — coma o
# punto y coma (autodetectado). ICS: subconjunto de RFC 5545 (DTSTART/SUMMARY/
# DESCRIPTION/RRULE con FREQ simple) — alcanza para exports de Google/Outlook.

_RECUR_ALIASES = {
    "": None, "una vez": None, "no": None, "none": None,
    "daily": "daily", "diario": "daily", "diaria": "daily",
    "weekly": "weekly", "semanal": "weekly",
    "monthly": "monthly", "mensual": "monthly",
    "yearly": "yearly", "anual": "yearly", "annual": "yearly",
}


def _parse_import_csv(text: str) -> list[dict]:
    first = text.splitlines()[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        raise ValueError("CSV sin encabezado")
    norm = {f: (f or "").strip().lower().replace("í", "i").replace("ó", "o")
                                        .replace("é", "e").replace("_", "_")
            for f in reader.fieldnames}
    if not {"fecha", "titulo"} <= set(norm.values()):
        raise ValueError("el CSV necesita encabezado con al menos: fecha,titulo "
                         "(opcionales: descripcion,tipo,de_quien,repeticion)")
    out = []
    for i, raw in enumerate(reader, start=2):  # fila 1 = encabezado
        row = {norm[k]: (v or "").strip() for k, v in raw.items() if k in norm}
        if not any(row.values()):
            continue  # fila vacía
        rec_raw = row.get("repeticion", "").strip().lower()
        rec = _RECUR_ALIASES.get(rec_raw, "__bad__")
        out.append({
            "fila": i,
            "fecha": row.get("fecha", ""),
            "titulo": row.get("titulo", ""),
            "descripcion": row.get("descripcion", ""),
            "tipo": (row.get("tipo") or "other").strip().lower() or "other",
            "handle": row.get("de_quien", "").lstrip("@").strip().lower() or None,
            "recur": None if rec == "__bad__" else rec,
            "error": (f"repetición desconocida: '{rec_raw}' (vacía|daily|weekly|"
                      "monthly|yearly o diario/semanal/mensual/anual)"
                      if rec == "__bad__" else None),
        })
    return out


def _ics_datetime(val: str) -> str | None:
    """DTSTART de ICS → ISO local. '20260801' → fecha; '...T210000' → fecha+hora;
    sufijo Z (UTC) → se convierte a hora AR (UTC-3)."""
    v = val.strip()
    utc = v.endswith("Z")
    v = v.rstrip("Z")
    try:
        if "T" in v:
            dt = datetime.strptime(v, "%Y%m%dT%H%M%S")
            if utc:
                dt -= timedelta(hours=3)
            return dt.strftime("%Y-%m-%dT%H:%M")
        return datetime.strptime(v, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _ics_unescape(val: str) -> str:
    return (val.replace("\\n", " ").replace("\\,", ",")
               .replace("\\;", ";").replace("\\\\", "\\").strip())


def _parse_ics(text: str) -> list[dict]:
    lines: list[str] = []
    for ln in text.replace("\r\n", "\n").split("\n"):  # unfold RFC 5545
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    freq_map = {"DAILY": "daily", "WEEKLY": "weekly",
                "MONTHLY": "monthly", "YEARLY": "yearly"}
    out: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        u = ln.strip()
        if u == "BEGIN:VEVENT":
            cur = {"fila": len(out) + 1, "fecha": "", "titulo": "", "descripcion": "",
                   "tipo": "other", "handle": None, "recur": None, "error": None}
        elif u == "END:VEVENT" and cur is not None:
            out.append(cur)
            cur = None
        elif cur is not None and ":" in u:
            key, _, val = u.partition(":")
            prop = key.split(";")[0].upper()
            if prop == "DTSTART":
                fecha = _ics_datetime(val)
                if fecha is None:
                    cur["error"] = cur["error"] or f"DTSTART ilegible: '{val}'"
                else:
                    cur["fecha"] = fecha
            elif prop == "SUMMARY":
                cur["titulo"] = _ics_unescape(val)
            elif prop == "DESCRIPTION":
                cur["descripcion"] = _ics_unescape(val)[:300]
            elif prop == "RRULE":
                parts = dict(p.split("=", 1) for p in val.split(";") if "=" in p)
                freq = parts.pop("FREQ", "").upper()
                parts.pop("WKST", None)  # inofensivo, se ignora
                extras = {k: v for k, v in parts.items() if not (k == "INTERVAL" and v == "1")}
                if freq not in freq_map or extras:
                    cur["error"] = cur["error"] or \
                        f"RRULE no soportada ('{val}'): solo FREQ simple daily/weekly/monthly/yearly"
                else:
                    cur["recur"] = freq_map[freq]
    return out


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
            elif self.path == "/botata_200.png":
                logo = REPO_DIR / "ui" / "botata_200.png"
                if logo.exists():
                    self._send(200, logo.read_bytes(), ctype="image/png")
                else:
                    self._send(404, {"error": "not found"})
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
                elif self.path == "/api/sources":
                    errs = store.write_sources(
                        body if isinstance(body, list) else body.get("sources", []))
                elif self.path == "/api/sources/test":
                    self._send(200, store.test_source(body))
                    return
                elif self.path == "/api/env":
                    errs = store.update_env(body)
                elif self.path == "/api/skills":
                    errs = store.set_skill_enabled(body.get("name", ""), bool(body.get("enabled")))
                elif self.path == "/api/memory":
                    errs = store.edit_memory(body)
                elif self.path == "/api/memory/user":
                    self._send(200, store.read_user_memory(body.get("handle")))
                    return
                elif self.path == "/api/memory/user/delete":
                    errs = store.delete_user_memory(body)
                elif self.path == "/api/memory/user/add":
                    errs = store.add_user_memory(body)
                elif self.path == "/api/behavior-file":
                    self._send(200, store.edit_behavior_file(body))
                    return
                elif self.path == "/api/preferences":
                    errs = store.edit_preferences(body)
                elif self.path == "/api/events":
                    errs = store.edit_events(body)
                elif self.path == "/api/events/announce":
                    errs = store.set_event_announce(body)
                elif self.path == "/api/events/import":
                    self._send(200, store.import_events(body))
                    return
                elif self.path == "/api/soul":
                    errs = store.write_soul(body.get("text", ""))
                elif self.path == "/api/moods":
                    errs = store.edit_mood(body)
                elif self.path == "/api/mood-today":
                    errs = store.set_mood_today(body)
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


def _port_busy(port: int) -> bool:
    """True si ya hay algo escuchando en 127.0.0.1:port. Necesario porque
    HTTPServer usa SO_REUSEADDR y en Windows eso deja bindear un puerto YA
    ocupado sin error: dos UIs terminaban pisándose en el 8787 en silencio."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve(base_dir: Path, port: int | None = 8787) -> ThreadingHTTPServer:
    """Crea el server (sin loop). El caller decide serve_forever/thread.

    port None = 8787 o el siguiente libre (hasta +20): varias UIs abiertas a la
    vez (una por instancia) no colisionan. Puerto explícito = estricto: si está
    ocupado, falla con mensaje claro — el caller lo pidió por algo.
    """
    store = ConfigStore(base_dir)
    if port is not None:
        if port != 0 and _port_busy(port):
            raise SystemExit(f"el puerto {port} ya está ocupado (¿otra UI abierta?) — "
                             "cerrala, elegí otro con --port, u omití --port para "
                             "que busque uno libre")
        return ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    for p in range(8787, 8807):
        if not _port_busy(p):
            return ThreadingHTTPServer(("127.0.0.1", p), make_handler(store))
    raise SystemExit("sin puertos libres en 8787-8806 — ¿cuántas UIs tenés abiertas? "
                     "(o pasá --port explícito)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Panel de configuración de botata (localhost)")
    parser.add_argument("--port", type=int, default=None,
                        help="puerto fijo (default: 8787 o el siguiente libre)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--instance", help="Directorio de la instancia a editar (default: raíz del repo)")
    args = parser.parse_args()
    httpd = serve(BASE_DIR, args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
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
