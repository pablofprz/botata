"""
botata.py — Phase 1 (LangGraph mention flow) + Feed reader

Modes:
  --mode admin_only   only responds to ADMIN_HANDLE (default)
  --mode open         responds to any mention

Usage:
  python botata.py --mode admin_only
  python botata.py --mode open
"""

import argparse
import base64
import json
import logging
import os
import random
import re
from html import unescape as _html_unescape
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from atproto import Client, models
from atproto.exceptions import NetworkError as BskyNetworkError, RequestException as BskyRequestException
from atproto_client.request import Request as AtprotoRequest
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from pydantic import AliasChoices, BaseModel, Field
from typing_extensions import TypedDict

import db as dbmod  # módulo de persistencia local (la var `db` es la conexión sqlite)
import budget as budgetmod  # guard de presupuesto diario de tokens
import clearsky as clearsky_mod  # proxy de la API pública de ClearSky ("quién me bloquea")
from tools import ToolRegistry, ToolContext, ToolResult, ToolHandler, Scope, ALL_SCOPES  # framework de tools
from skills import skills_prompt_block, get_skill_body  # workspace de skills (T26)
import moods as moodmod  # estados de ánimo del bot (registro conductual por día)
from scheduler import PeriodicTask, apply_tasks_config, run_due  # tareas periódicas (T27)
from router import ModelRouter, RoleLLM, build_router  # router de modelos + fallbacks

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("botata")

# Silenciar el ruido de librerías HTTP ('POST .../chat/completions 200 OK' por
# cada llamada al LLM no aporta nada). Errores/warnings siguen pasando.
for _noisy in ("httpx", "httpcore", "openai", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def log_llm_context(label: str, system: str, user: str) -> None:
    """Imprime el contexto completo que se le manda al LLM (toggle LOG_CONTEXT).
    Un solo log.info multilinea con delimitadores para poder grepear/plegar."""
    if not LOG_CONTEXT:
        return
    log.info(
        "\n╭──── contexto LLM: %s ────\n[SYSTEM]\n%s\n\n[USER]\n%s\n╰──── fin contexto: %s ────",
        label, system, user, label,
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).resolve().parent.parent  # src/ -> raíz del repo
CONFIG_DIR  = BASE_DIR / "config"
CONTEXT_DIR = BASE_DIR / "context"
PROMPTS_DIR = BASE_DIR / "prompts"
SKILLS_DIR  = BASE_DIR / "skills"   # T26: workspace de skills en markdown
MOODS_DIR   = BASE_DIR / "moods"    # estados de ánimo del bot (markdown)
POSTED_DIR  = BASE_DIR / "posted"
FEEDS_DIR   = CONTEXT_DIR / "feeds"

POSTED_DIR.mkdir(exist_ok=True)
FEEDS_DIR.mkdir(exist_ok=True)

DB_PATH        = POSTED_DIR  / "botata.db"
SETTINGS_PATH  = CONFIG_DIR  / "settings.json"
MEMORY_PATH    = CONTEXT_DIR / "MEMORY.md"
# Heartbeat: tres archivos, tres roles distintos (NO redundantes):
#   prompts/heartbeat_engine.md    → el motor: framing + reglas duras del pase (invariantes).
#   prompts/heartbeat_checklist.md → la checklist de tareas de base, versionada (mecanismo PRINCIPAL).
#   context/heartbeat_override.md  → override runtime que escribe set_heartbeat (AUXILIAR); pisa la checklist.
HEARTBEAT_OVERRIDE_PATH  = CONTEXT_DIR / "heartbeat_override.md"   # override runtime del admin (vía set_heartbeat)
HEARTBEAT_CHECKLIST_PATH = PROMPTS_DIR / "heartbeat_checklist.md"  # checklist de base versionada

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(BASE_DIR / ".env")


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_text(path: Path, default: str = "") -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return default


# ─── T3: el bot sabe la fecha ───────────────────────────────────────────────
# TZ America/Argentina/Buenos_Aires. Windows no trae tzdata → fallback a offset
# fijo UTC-3 (Argentina no usa DST). Nombres en español, independientes del locale.
try:
    from zoneinfo import ZoneInfo
    _AR_TZ: object = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:  # pragma: no cover - falta tzdata (Windows)
    _AR_TZ = timezone(timedelta(hours=-3))

_DIAS_ES  = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def now_ar() -> datetime:
    """Datetime actual en hora de Argentina."""
    return datetime.now(_AR_TZ)


def current_datetime_line() -> str:
    """Línea de fecha/hora para inyectar en los prompts de reasoning (T3)."""
    n = now_ar()
    return (
        f"Fecha y hora actual: {_DIAS_ES[n.weekday()]} {n.day} de "
        f"{_MESES_ES[n.month - 1]} de {n.year}, {n:%H:%M} (hora de Argentina). "
        "Esta es la fecha de HOY. Cualquier fecha que aparezca en tu memoria o "
        "contexto pertenece al pasado; no la confundas con el presente."
    )


settings = load_json(SETTINGS_PATH)

BSKY_HANDLE        : str  = settings["BOT_HANDLE"]
BSKY_PASSWORD      : str  = os.environ["BSKY_PASSWORD"]
OPENROUTER_API_KEY : str  = os.environ["OPENROUTER_API_KEY"]
BRAVE_API_KEY      : str | None = os.environ.get("BRAVE_API_KEY")  # opcional (tool web_search, T8)
ADMIN_HANDLE       : str  = settings["ADMIN_HANDLE"]   # owner (primario, para mensajes)
# Admins = owner + los handles extra de ADMIN_HANDLES (lista opcional en settings.json).
# Todos tienen acceso total (Scope.ADMIN). Para agregar un admin: sumá su handle a
# ADMIN_HANDLES. Es un PROTECTED_SETTING: no se puede cambiar por comando del bot.
ADMIN_HANDLES : frozenset[str] = frozenset(
    [ADMIN_HANDLE, *(settings.get("ADMIN_HANDLES") or [])]
)


def is_admin_handle(handle: str | None) -> bool:
    """True si `handle` es admin (owner o de la lista ADMIN_HANDLES)."""
    return bool(handle) and handle in ADMIN_HANDLES
REASONING_MODEL    : str  = settings.get("REASONING_MODEL", "deepseek/deepseek-r1")
LITE_MODEL         : str  = settings.get("LITE_MODEL") or REASONING_MODEL
IMAGE_MODEL        : str  = settings.get("IMAGE_MODEL", "google/gemini-2.5-flash")
OPENAI_ENDPOINT    : str  = settings.get("OPENAI_ENDPOINT", "https://openrouter.ai/api/v1")
POLL_INTERVAL      : int  = settings.get("POLL_INTERVAL_SECONDS", 60)
FEEDS_CONFIG       : list = settings.get("FEEDS", [])
TOOLS_CONFIG       : dict = settings.get("TOOLS", {})
MCP_CONFIG         : dict = settings.get("MCP", {})
TASKS_CONFIG       : dict = settings.get("TASKS", {})
MODELS_CONFIG      : dict | None = settings.get("MODELS")


def _load_news_sources() -> list[dict]:
    """Carga config/news_sites.json. Acepta strings (back-compat, mode=post) u objetos
    {url, title, description, mode, enabled, interval_hours}. Tagea cada fuente con `host`."""
    try:
        raw = load_json(CONFIG_DIR / "news_sites.json")
    except Exception:
        return []
    out: list[dict] = []
    for e in raw:
        if isinstance(e, str):
            e = {"url": e, "mode": "post"}
        if not e.get("url"):
            continue
        host = urllib.parse.urlparse(e["url"]).netloc.replace("www.", "") or e["url"]
        out.append({**e, "host": host})
    return out


NEWS_SOURCES : list = _load_news_sources()
# Master toggle del posteo de noticias (outward-facing). OFF por default: el admin lo prende.
NEWS_ENABLED : bool = bool(settings.get("NEWS_ENABLED", False))

# Leer la media (imágenes/video/GIF) de los posts del hilo: corre el modelo
# vision (rol image_describe) sobre el frame/thumbnail y suma la descripción al
# contexto que ve el LLM. Sin esto, un post que es solo un video o un GIF le
# llega vacío al bot. Best-effort y toggleable (cuesta 1 llamada vision por
# pieza de media). ON por default.
READ_THREAD_MEDIA : bool = bool(settings.get("READ_THREAD_MEDIA", True))

# Loguear el contexto COMPLETO (system + user) que se le manda al LLM al
# generar una respuesta — visibilidad de qué está viendo el bot realmente.
# Verboso a propósito; apagable por config.
LOG_CONTEXT : bool = bool(settings.get("LOG_CONTEXT", True))

# Estados de ánimo (moods): un registro afectivo que tiñe el tono del bot por día.
# {enabled, mode: manual|auto, manual: {fixed, schedule}, susceptibility,
# hysteresis_hours}. En modo auto el bot además puede CAMBIAR de mood dentro del
# día vía la tool choose_mood: susceptibility (0–1) calibra cuán fácil cede (0 =
# la tool ni se ofrece) y hysteresis_hours es el mínimo entre cambios (anti-
# oscilación). Los moods en sí viven en moods/*.md. Off por default. README de moods/.
MOODS_CONFIG : dict = settings.get("MOODS", {})

# Gustos y disgustos del bot (tabla preferences): capa de identidad EDITABLE,
# separada de SOUL.md a propósito. PREFS.mode gobierna qué puede hacer el BOT
# solo (el admin siempre puede todo, por tools admin o UI):
#   manual (default) = el bot no toca nada · add_only = puede agregar, no sacar
#   · full_auto = agrega y saca (pero jamás los source='admin').
PREFS_CONFIG : dict = settings.get("PREFS", {})
PREFS_MODE   : str  = str(PREFS_CONFIG.get("mode", "manual")).lower()

# Quién puede agendarle ACCIONES al bot (eventos kind='bot_action': "posteá X a
# tal hora"). 'admin' (default) = solo el admin; 'any' = cualquier usuario.
# Superficie de prompt injection en bot público → default cerrado. Semilla del
# futuro scope de permisos por usuario.
BOT_ACTIONS_FROM : str = str(settings.get("BOT_ACTIONS_FROM", "admin")).lower()

# Grupos de usuarios: {"music_users": ["handle1", ...]}. Una tool con
# TOOLS.<name>.groups = ["music_users"] solo la pueden gatillar (en scope reply)
# los miembros de esos grupos; sin `groups` la tool es para todos (back-compat).
# El admin bypassea todo check. Editar membresías = UI/archivo, no por comando.
USER_GROUPS : dict = settings.get("USER_GROUPS", {})

# Playlist comunitaria de recomendaciones (tool add_music_recommendation).
# Solo el ID (no la URL completa). Escribir requiere el token de USUARIO de
# spotify_auth.py (autorización única del admin) — Client Credentials no puede.
SPOTIFY_PLAYLIST_ID : str = str(settings.get("SPOTIFY_PLAYLIST_ID", "")).strip()

# Presupuesto diario de tokens (guard económico, portado de maripobot).
# {enabled, daily_usd, announce} — editable desde la UI (python config_ui.py).
# Quemado el budget del día → el loop saltea TODAS las tareas hasta mañana.
BUDGET_CONFIG : dict = settings.get("BUDGET", {})

# Back-compat del router: si no hay sección MODELS, se derivan los aliases de estos.
_LEGACY_MODELS = {
    "base_url":  OPENAI_ENDPOINT,
    "api_key":   OPENROUTER_API_KEY,
    "reasoning": REASONING_MODEL,
    "lite":      LITE_MODEL,
    "vision":    IMAGE_MODEL,
}

# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    """Conexión a botata.db con sqlite-vec cargado y esquema completo (delegado a dbmod)."""
    conn = dbmod.init_db(DB_PATH)
    _migrate_memory_file(conn)
    return conn


def _migrate_memory_file(conn: sqlite3.Connection) -> None:
    """One-shot: ingiere el viejo context/MEMORY.md a la tabla bot_memory.

    MEMORY.md queda RETIRADO como fuente (la DB es el source of truth único);
    el archivo no se borra pero ya nadie lo lee. Guard por kv para no
    re-ingestar. Formato del archivo: headers `## YYYY-MM-DD` + bullets `- …`
    (la fecha del header se preserva como created_at)."""
    if dbmod.kv_get(conn, "memory_file_migrated"):
        return
    try:
        raw = load_text(MEMORY_PATH)
        n, current_date = 0, None
        for line in raw.splitlines():
            line = line.strip()
            m = re.match(r"^#{1,6}\s*(\d{4}-\d{2}-\d{2})\s*$", line)
            if m:
                current_date = m.group(1)
                continue
            if line.startswith(("- ", "* ")):
                text = line[2:].strip()
                if text and dbmod.add_bot_memory(
                        conn, text, source="migration:MEMORY.md",
                        created_at=current_date) is not None:
                    n += 1
        dbmod.kv_set(conn, "memory_file_migrated", now_ar().isoformat())
        if n:
            log.info("bot_memory: migradas %d entradas de context/MEMORY.md (archivo retirado)", n)
    except Exception:
        # best-effort: un archivo roto no debe impedir arrancar; se reintenta
        # el próximo arranque (el kv solo se setea si la pasada terminó).
        log.warning("no pude migrar context/MEMORY.md a bot_memory", exc_info=True)


def has_replied(conn: sqlite3.Connection, uri: str) -> bool:
    """
    True = skip this post.
    'replied', 'pending', 'ignored' → skip.
    'failed' → retry.
    missing  → process.
    """
    row = conn.execute(
        "SELECT status FROM replied_posts WHERE uri = ?", (uri,)
    ).fetchone()
    if row is None:
        return False
    return row[0] in ("replied", "pending", "ignored")


def mark_pending(conn: sqlite3.Connection, uri: str, cid: str, author: str, mode: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO replied_posts (uri, cid, author, status, replied_at, mode) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (uri, cid, author, datetime.now(timezone.utc).isoformat(), mode),
    )
    conn.commit()


def update_status(conn: sqlite3.Connection, uri: str, status: str) -> None:
    conn.execute("UPDATE replied_posts SET status = ? WHERE uri = ?", (status, uri))
    conn.commit()


def log_bot_post(
    conn: sqlite3.Connection,
    uri: str,
    in_reply_to: str | None,
    reply_to_handle: str | None,
    text: str,
) -> None:
    """Registra un post publicado por el bot en bot_posts (log de salida)."""
    conn.execute(
        "INSERT OR IGNORE INTO bot_posts (uri, in_reply_to, reply_to_handle, text) "
        "VALUES (?, ?, ?, ?)",
        (uri, in_reply_to, reply_to_handle, text),
    )
    conn.commit()


def recent_bot_posts(conn: sqlite3.Connection, limit: int = 10) -> list[str]:
    """Últimos textos posteados por el bot (para dedup y para evitar repetirse)."""
    rows = conn.execute(
        "SELECT text FROM bot_posts WHERE text IS NOT NULL "
        "ORDER BY posted_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def recent_bot_activity(
    conn: sqlite3.Connection, limit: int = 8, reply_to: str | None = None
) -> list[dict]:
    """Actividad reciente del bot (T25: que pueda hablar de lo que hizo).

    Devuelve posts con fecha + a quién le respondió. `reply_to` filtra por handle
    respondido (ej. "¿qué le contestaste a @X?")."""
    q      = "SELECT posted_at, text, reply_to_handle, in_reply_to FROM bot_posts WHERE text IS NOT NULL"
    params: list = []
    if reply_to:
        q += " AND reply_to_handle = ?"
        params.append(reply_to)
    q += " ORDER BY posted_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, tuple(params)).fetchall()]


def _norm_text(s: str) -> str:
    """Normaliza para comparación de duplicados: minúsculas, espacios colapsados."""
    return " ".join(s.lower().split())


def append_feed_summary(feed_name: str, summary: str) -> None:
    """Appendea un resumen timestamped a context/feeds/{feed_name}.md (memoria del feed)."""
    path      = FEEDS_DIR / f"{feed_name}.md"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    existing  = path.read_text(encoding="utf-8").rstrip() if path.exists() else f"# {feed_name}"
    path.write_text(existing + f"\n\n## {timestamp}\n{summary}\n", encoding="utf-8")
    log.info("Feed %s: summary saved to %s", feed_name, path.name)


def get_feed_last_run(conn: sqlite3.Connection, feed_name: str) -> str | None:
    """Returns the ISO timestamp of the last time this feed was processed, or None."""
    row = conn.execute(
        "SELECT last_run FROM feed_cursors WHERE feed_name = ?", (feed_name,)
    ).fetchone()
    return row[0] if row else None


def save_feed_last_run(conn: sqlite3.Connection, feed_name: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO feed_cursors (feed_name, last_run) VALUES (?, ?)",
        (feed_name, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Profile index
# ---------------------------------------------------------------------------

# Pydantic models
# ---------------------------------------------------------------------------

class MentionClassification(BaseModel):
    model_config = {"populate_by_name": True}

    is_admin_command: bool = Field(
        alias="is_command",
        default=False,
        description=(
            "True if the message is a bot command: starts with '/' (e.g. /remember, /debug) "
            "OR is an instruction about the bot's own configuration/behavior/memory "
            "(turn features on/off, change settings, show config, remember something)."
        ),
    )
    command: str | None = Field(
        default=None,
        description=(
            "Command name without the slash (e.g. 'remember', 'debug'), or 'config' for "
            "natural-language configuration instructions. None if not a command."
        ),
    )
    skip: bool = Field(
        default=False,
        description="True if the bot should not reply (spam, self-mention, nonsensical)",
    )
    is_block_query: bool = Field(
        default=False,
        description=(
            "True if the user is asking who blocks them or who they block: "
            "'/bloques', '/blocks', 'quién me bloquea', 'me tienen bloqueado', "
            "'who blocks me'. Conservative: when in doubt, false."
        ),
    )
    is_role_query: bool = Field(
        default=False,
        description=(
            "True if the user is asking about their own role/permissions with the bot: "
            "'/check-role', '/rol', '/permisos', 'qué permisos tengo', 'cuál es mi rol', "
            "'soy admin?', 'qué puedo hacer', 'what can I do'. Conservative: when in doubt, false."
        ),
    )


class BotReply(BaseModel):
    text: str = Field(
        default="",
        validation_alias=AliasChoices("text", "message"),
        description="Reply text. Max 300 chars. No hashtags. Rioplatense casual.",
    )
    should_update_profile: bool = Field(
        default=False,
        description="True if something notable about the user was revealed in this interaction",
    )
    image_search_query: str | None = Field(
        default=None,
        description=(
            "If the user asked for an image (meme, foto, etc.), the search query "
            "to find a matching image in the catalog. None if no image is needed. "
            "Use keywords in Spanish, e.g. 'gato enojado', 'meme programación'."
        ),
    )


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class MentionState(TypedDict):
    # Set before the graph runs — never modified by nodes
    mention_uri      : str
    mention_cid      : str
    mention_text     : str
    author_handle    : str
    thread_context   : str
    thread_root_uri  : str
    thread_root_cid  : str
    mode             : str
    is_admin         : bool

    # Filled in by nodes
    classification   : MentionClassification | None
    reply_text       : str | None
    should_update_profile : bool
    image_path       : str | None   # path de imagen a adjuntar al reply
    posted_reply_uri : str | None
    error            : str | None


# ---------------------------------------------------------------------------
# Rich text: facets de link + tarjeta de preview (embed external)
# ---------------------------------------------------------------------------
# Bluesky no autodetecta links: un URL en texto plano no es clickable ni genera
# preview. Hay que declarar facets (para el link) y un embed external (la tarjeta
# con título/descripción/miniatura, vía OpenGraph del sitio). Genérico: sirve para
# cualquier link que el bot incluya (Spotify, YouTube, noticias, web).

# URL http(s) hasta el primer espacio; recortamos puntuación de cierre al final.
_URL_RE = re.compile(r"https?://[^\s]+")
_OG_UA = "Mozilla/5.0 (compatible; botata/1.0; +https://bsky.app)"


def _find_urls_with_offsets(text: str) -> list[tuple[str, int, int]]:
    """URLs en `text` con offsets en BYTES UTF-8 (lo que exige facet.index de Bluesky)."""
    out: list[tuple[str, int, int]] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(").,;]'\"")  # no tragarse puntuación de cierre
        byte_start = len(text[: m.start()].encode("utf-8"))
        byte_end = byte_start + len(url.encode("utf-8"))
        out.append((url, byte_start, byte_end))
    return out


def build_link_facets(text: str):
    """Facets de tipo link para cada URL del texto → el URL queda clickable."""
    facets = []
    for url, bs, be in _find_urls_with_offsets(text):
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Link(uri=url)],
                index=models.AppBskyRichtextFacet.ByteSlice(byte_start=bs, byte_end=be),
            )
        )
    return facets


# Un @handle de Bluesky es un nombre de dominio (ppolci.com, user.bsky.social).
# El @ debe estar al inicio o precedido por un no-word char (evita mails: foo@bar.com).
# Trabaja sobre BYTES: los offsets de facet.index se miden en bytes UTF-8.
_MENTION_RE = re.compile(
    rb"(?:^|(?<=\W))(@([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,62}[a-zA-Z0-9])?"
    rb"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,62}[a-zA-Z0-9])?)+))"
)


def _find_mentions_with_offsets(text: str) -> list[tuple[str, int, int]]:
    """(handle, byte_start, byte_end) por cada @handle. Los offsets abarcan el @."""
    out: list[tuple[str, int, int]] = []
    data = text.encode("utf-8")
    for m in _MENTION_RE.finditer(data):
        handle = m.group(2).decode("utf-8")  # grupo 2 = handle sin el '@'
        out.append((handle, m.start(1), m.end(1)))
    return out


def build_mention_facets(text: str, resolver):
    """Facets de tipo mention para cada @handle → el usuario queda linkeable.

    `resolver(handle) -> did | None`. Un handle que no resuelve (typo, cuenta
    borrada) se deja como texto plano (sin facet), no rompe el post.
    """
    facets = []
    for handle, bs, be in _find_mentions_with_offsets(text):
        did = resolver(handle)
        if not did:
            continue
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Mention(did=did)],
                index=models.AppBskyRichtextFacet.ByteSlice(byte_start=bs, byte_end=be),
            )
        )
    return facets


def _meta_content(html: str, prop: str) -> str | None:
    """Extrae el content de un <meta property|name="prop"> (orden de atributos indistinto)."""
    p = re.escape(prop)
    for pat in (
        rf'<meta[^>]+(?:property|name)=["\']{p}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{p}["\']',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return _html_unescape(m.group(1)).strip()
    return None


def _youtube_oembed(url: str) -> dict | None:
    """YouTube sirve una página de consentimiento sin OG tags a los bots; su oEmbed
    devuelve título, canal y miniatura sin scrapear. Devuelve {title, description, image}."""
    if not re.search(r"(youtube\.com/watch|youtu\.be/)", url):
        return None
    try:
        api = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"})
        req = urllib.request.Request(api, headers={"User-Agent": _OG_UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        log.debug("YouTube oEmbed falló %s: %s", url, e)
        return None
    return {"title": data.get("title") or url,
            "description": data.get("author_name") or "",
            "image": data.get("thumbnail_url")}


def _fetch_og_card(url: str) -> dict | None:
    """Baja el OpenGraph del link (best-effort, stdlib). Devuelve {title, description, image}."""
    yt = _youtube_oembed(url)
    if yt:
        return yt
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _OG_UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if "html" not in (resp.headers.get("Content-Type") or "").lower():
                return None
            html = resp.read(200_000).decode("utf-8", "ignore")
    except Exception as e:
        log.debug("OG fetch falló %s: %s", url, e)
        return None
    title = _meta_content(html, "og:title") or _meta_content(html, "twitter:title")
    desc = _meta_content(html, "og:description") or _meta_content(html, "twitter:description") or ""
    image = _meta_content(html, "og:image") or _meta_content(html, "twitter:image")
    if not title:
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        title = _html_unescape(m.group(1)).strip() if m else url
    return {"title": title, "description": desc, "image": image}


# ---------------------------------------------------------------------------
# Lectura de media del hilo (imágenes/video/GIF)
# ---------------------------------------------------------------------------
# Los posts que ve el bot pueden traer media. `_extract_text` solo devuelve el
# texto → un post que es SOLO un video o un GIF le llega vacío. Acá aplanamos el
# embed VIEW a piezas describibles y corremos vision sobre el frame/thumbnail
# (los GIF de Bluesky son embeds `external` de Tenor; los videos exponen un
# `thumbnail` de portada; las imágenes, `fullsize`). Best-effort: todo falla en
# silencio y el post simplemente pierde la anotación.

_MEDIA_MAX_ITEMS = 4  # tope de piezas a describir por post (evita blow-ups de costo)
_VISION_DESCRIBE_SYSTEM = (
    "Describí la imagen en UNA sola frase concisa en español rioplatense "
    "(máx. 25 palabras). Si es un fotograma de video o un GIF, describí lo que "
    "se ve en ese cuadro. Sin preámbulos ni comillas."
)


def _sniff_image_mime(blob: bytes) -> str:
    """MIME por magic bytes (suficiente para lo que sirve el CDN de Bluesky)."""
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _iter_media_views(embed) -> list[dict]:
    """Aplana un embed VIEW a piezas de media visual.

    Devuelve dicts {kind, image_url?, alt?, title?}. `kind` ∈
    imagen|video|GIF|enlace. `image_url` es la URL del CDN a describir con vision
    (None si no hay cuadro que mirar, ej. enlace sin thumbnail)."""
    if embed is None:
        return []
    pt = getattr(embed, "py_type", "") or ""
    out: list[dict] = []
    if pt == "app.bsky.embed.images#view":
        for img in getattr(embed, "images", None) or []:
            out.append({
                "kind": "imagen",
                "image_url": getattr(img, "fullsize", None) or getattr(img, "thumb", None),
                "alt": (getattr(img, "alt", "") or "").strip(),
            })
    elif pt == "app.bsky.embed.video#view":
        out.append({
            "kind": "video",
            "image_url": getattr(embed, "thumbnail", None),  # frame de portada
            "alt": (getattr(embed, "alt", "") or "").strip(),
        })
    elif pt == "app.bsky.embed.external#view":
        ext = getattr(embed, "external", None)
        if ext is not None:
            uri = (getattr(ext, "uri", "") or "")
            low = uri.lower()
            is_gif = ".gif" in low or "tenor.com" in low or "giphy.com" in low
            out.append({
                "kind": "GIF" if is_gif else "enlace",
                "image_url": getattr(ext, "thumb", None),
                "title": (getattr(ext, "title", "") or "").strip(),
            })
    elif pt == "app.bsky.embed.recordWithMedia#view":
        # post con texto/cita + media adjunta → describimos solo la media
        out.extend(_iter_media_views(getattr(embed, "media", None)))
    # app.bsky.embed.record#view (cita a otro post) → no se describe acá
    return out[:_MEDIA_MAX_ITEMS]


def _vision_describe_url(vision_llm, url: str) -> str:
    """Baja la imagen del CDN y la describe con el modelo vision. '' si falla."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _OG_UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            blob = r.read(4_000_000)
        data_url = f"data:{_sniff_image_mime(blob)};base64,{base64.b64encode(blob).decode('ascii')}"
        text = vision_llm.chat(
            [
                {"role": "system", "content": _VISION_DESCRIBE_SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "Describí esta imagen."},
                ]},
            ],
            max_tokens=120,
        )
        return " ".join((text or "").split())[:220]
    except Exception as e:
        log.debug("vision describe falló %s: %s", url, e)
        return ""


def _format_media_piece(kind: str, desc: str, extra: str) -> str:
    """Etiqueta legible de una pieza de media para el contexto del LLM."""
    if kind == "GIF":
        bits = [b for b in (extra, desc) if b]
        return f"[GIF: {' — '.join(bits)}]" if bits else "[GIF adjunto]"
    if kind == "video":
        core = desc or extra
        return f"[video, primer frame: {core}]" if core else "[video adjunto]"
    if kind == "enlace":
        return f"[enlace: {extra or desc}]" if (extra or desc) else "[enlace adjunto]"
    # imagen
    core = desc or extra
    return f"[imagen: {core}]" if core else "[imagen adjunta]"


def make_media_describer(vision_llm):
    """Fábrica: devuelve fn(post_view)->str que anota la media del post con vision.

    Best-effort: cualquier pieza que falle se saltea; '' si el post no trae media
    describible. `vision_llm` = RoleLLM(router, 'image_describe')."""
    def describe(post_view) -> str:
        try:
            items = _iter_media_views(getattr(post_view, "embed", None))
        except Exception:
            log.debug("iter_media_views falló", exc_info=True)
            return ""
        pieces: list[str] = []
        for it in items:
            desc = ""
            if it.get("image_url"):
                desc = _vision_describe_url(vision_llm, it["image_url"])
            extra = it.get("title") or it.get("alt") or ""
            pieces.append(_format_media_piece(it["kind"], desc, extra))
        return " ".join(pieces)

    return describe


# ---------------------------------------------------------------------------
# Bluesky client
# ---------------------------------------------------------------------------

class BskyClient:

    # El default de httpx (5s connect) es muy justo para la red de prod;
    # un ConnectTimeout transitorio en el arranque no debe matar el proceso.
    _LOGIN_RETRIES = 4
    _LOGIN_BACKOFF = 5  # segundos, exponencial: 5, 10, 20, 40

    def __init__(self, handle: str, password: str):
        self.handle = handle
        self._did_cache: dict[str, str | None] = {}
        self._media_describer = None  # fn(post_view)->str; inyectado en el arranque
        request = AtprotoRequest(timeout=httpx.Timeout(30.0, connect=15.0))
        self._client = Client(request=request)
        for attempt in range(1, self._LOGIN_RETRIES + 1):
            try:
                self._client.login(handle, password)
                break
            except BskyNetworkError as e:
                if attempt == self._LOGIN_RETRIES:
                    raise
                wait = self._LOGIN_BACKOFF * (2 ** (attempt - 1))
                log.warning("Bluesky login failed (%s), retry %d/%d in %ds",
                            type(e).__name__, attempt, self._LOGIN_RETRIES, wait)
                time.sleep(wait)
        log.info("Logged in as %s", handle)

    def get_mentions(self) -> list[dict]:
        """Fetch recent mention and reply notifications.

        Dedup is delegated to the DB (`has_replied`) — the single source of
        truth. We intentionally do NOT filter on `is_read`: Bluesky's seen
        marker is global and coarse, and relying on it silently dropped
        mentions that failed mid-processing (their DB 'failed' → retry path
        was unreachable). Returning read notifications too lets `has_replied`
        decide, so a transient failure gets retried instead of lost.
        """
        notifs = self._client.app.bsky.notification.list_notifications(params={"limit": 25})
        mentions = []
        for n in notifs.notifications:
            if n.reason in ("mention", "reply"):
                mentions.append({
                    "uri"           : n.uri,
                    "cid"           : n.cid,
                    "author_handle" : n.author.handle,
                    "text"          : self._extract_text(n.record),
                })
        return mentions

    def mark_all_read(self) -> None:
        self._client.app.bsky.notification.update_seen(
            {"seenAt": datetime.now(timezone.utc).isoformat()}
        )

    def block_user(self, handle: str) -> bool:
        """Bloquea a un usuario creando el record `app.bsky.graph.block`. True si OK.

        T10: NO se testea en vivo (bloquearía una cuenta real). Verificado solo la
        forma de la API del SDK; la ejecución real queda pendiente de validar a mano.
        """
        profile = self.get_profile(handle)
        if not profile:
            log.error("block_user: no pude resolver @%s", handle)
            return False
        try:
            self._client.app.bsky.graph.block.create(
                self._client.me.did,
                models.AppBskyGraphBlock.Record(
                    subject=profile.did,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
            log.info("block_user: bloqueado @%s (%s)", handle, profile.did)
            return True
        except Exception as e:
            log.error("block_user: falló bloquear @%s: %s", handle, e)
            return False

    def get_profile(self, handle: str):
        """Fetch the full Bluesky profile for a handle. Returns None on error."""
        try:
            return self._client.app.bsky.actor.get_profile({"actor": handle})
        except Exception as e:
            log.warning("Could not fetch profile for %s: %s", handle, e)
            return None

    def resolve_did(self, handle: str) -> str | None:
        """Resolve a handle to its Bluesky DID (stable across handle changes)."""
        try:
            return self._client.app.bsky.actor.get_profile({"actor": handle}).did
        except Exception as e:
            log.warning("Could not resolve DID for %s: %s", handle, e)
            return None

    def _resolve_did_cached(self, handle: str) -> str | None:
        """resolve_did con cache por proceso (evita un fetch por cada @mención)."""
        h = handle.lstrip("@").lower()
        if h not in self._did_cache:
            self._did_cache[h] = self.resolve_did(h)
        return self._did_cache[h]

    def _all_facets(self, text: str):
        """Facets de links + menciones combinados (offsets en bytes, no se pisan)."""
        facets = build_link_facets(text)
        facets += build_mention_facets(text, self._resolve_did_cached)
        return facets or None

    def get_profile_handles(self, dids: list[str]) -> dict[str, str]:
        """Map DIDs → handles in batches of 25 (AppView getProfiles limit).
        DIDs that don't resolve (deleted/deactivated accounts) are omitted."""
        out: dict[str, str] = {}
        for i in range(0, len(dids), 25):
            batch = dids[i : i + 25]
            try:
                resp = self._client.app.bsky.actor.get_profiles({"actors": batch})
                for p in resp.profiles:
                    out[p.did] = p.handle
            except Exception as e:
                log.warning("get_profiles failed for batch of %d: %s", len(batch), e)
        return out

    def set_media_describer(self, fn) -> None:
        """Inyecta el describidor de media (vision). Sin él, la media no se anota."""
        self._media_describer = fn

    def _describe_media(self, post_view) -> str:
        """Descripción de la media del post (vía el describidor inyectado). '' si
        no hay describidor o no hay media. Jamás lanza."""
        if self._media_describer is None:
            return ""
        try:
            return self._media_describer(post_view)
        except Exception:
            log.debug("describe_media falló", exc_info=True)
            return ""

    def _thread_context(self, leaf) -> tuple[str, str, str]:
        """Parent chain of `leaf` as chronological context (leaf excluded),
        plus (root_uri, root_cid). Shared by get_thread_info and get_mention_by_uri.

        The leaf (the current mention) is intentionally excluded: GenerateReplyNode
        appends it separately as `current`. Including it here doubled the user's
        message in the LLM query (token waste + the bot perceived "publicaciones repetidas").
        """
        lines: list[str] = []
        root_uri = leaf.post.uri
        root_cid = leaf.post.cid
        node = getattr(leaf, "parent", None)
        while node is not None:
            if hasattr(node, "post"):
                text = self._extract_text(node.post.record)
                media = self._describe_media(node.post)
                if media:
                    text = f"{text} {media}".strip()
                lines.append(f"{node.post.author.handle}: {text}")
                root_uri = node.post.uri
                root_cid = node.post.cid
            node = getattr(node, "parent", None)
        lines.reverse()
        return "\n".join(lines), root_uri, root_cid

    def get_thread_info(self, uri: str, cid: str) -> tuple[str, str, str, str]:
        """
        Returns (context_text, root_uri, root_cid, leaf_media).
        context_text: chronological PRIOR conversation for the LLM (parent chain only).
        root_uri/cid: the original post that started the thread — required by Bluesky
                      to maintain thread structure when replying.
        leaf_media:   descripción vision de la media del post actual (la mención),
                      '' si no trae media. El poll la anexa al texto de la mención
                      (que viene del record de la notificación, sin la vista/CDN).
        """
        try:
            resp = self._client.app.bsky.feed.get_post_thread({"uri": uri})
        except Exception as e:
            log.warning("Could not fetch thread for %s: %s", uri, e)
            return "", uri, cid, ""
        leaf = resp.thread
        if not hasattr(leaf, "post"):
            return "", uri, cid, ""
        ctx, root_uri, root_cid = self._thread_context(leaf)
        return ctx, root_uri, root_cid, self._describe_media(leaf.post)

    def get_mention_by_uri(self, uri: str) -> dict | None:
        """Reconstruct a mention dict (with thread context) from a single post URI.
        Used to retry failed/stale mentions on startup — the poll only sees the latest
        25 notifications, so older failed mentions must be refetched by URI. Returns
        None if the post is deleted/gone (caller should mark it 'ignored')."""
        try:
            resp = self._client.app.bsky.feed.get_post_thread({"uri": uri})
        except Exception as e:
            log.warning("get_mention_by_uri: fetch failed for %s: %s", uri, e)
            return None
        leaf = resp.thread
        if not hasattr(leaf, "post"):
            return None
        post = leaf.post
        context, root_uri, root_cid = self._thread_context(leaf)
        text = self._extract_text(post.record)
        media = self._describe_media(post)
        if media:
            text = f"{text} {media}".strip()
        return {
            "uri"            : post.uri,
            "cid"            : post.cid,
            "author_handle"  : post.author.handle,
            "text"           : text,
            "thread_context" : context,
            "thread_root_uri": root_uri,
            "thread_root_cid": root_cid,
        }

    def resolve_list_uri(self, uri: str) -> str:
        """
        Convert at://handle/app.bsky.graph.list/xxx to at://did:plc:.../app.bsky.graph.list/xxx.
        get_list_feed requires a DID-based URI — handles don't resolve in that endpoint.
        If the URI already contains a DID, returns it unchanged.
        """
        # at://did:plc:xxx/... — already resolved
        if uri.startswith("at://did:"):
            return uri

        # at://handle.bsky.social/app.bsky.graph.list/xxx
        parts = uri.removeprefix("at://").split("/", 1)
        if len(parts) != 2:
            log.warning("resolve_list_uri: unexpected URI format %s", uri)
            return uri

        handle, rest = parts
        try:
            profile = self._client.app.bsky.actor.get_profile({"actor": handle})
            resolved = f"at://{profile.did}/{rest}"
            log.info("Resolved %s → %s", uri, resolved)
            return resolved
        except Exception as e:
            log.error("Could not resolve handle %s: %s", handle, e)
            return uri

    def _paginate_posts(self, fetch_page, label: str, since: datetime | None,
                        limit: int = 50) -> list[dict]:
        """Paginación + extracción común a cualquier fuente de feed (T7).

        `fetch_page(cursor, limit)` devuelve una respuesta del SDK con `.feed`
        (items con `.post`) y opcional `.cursor`. Se detiene al alcanzar posts
        anteriores a `since`, al quedarse sin cursor, o al tope de páginas.
        """
        posts: list[dict] = []
        cursor            = None
        max_pages         = 5  # safety cap — avoids infinite loops on huge feeds

        for page in range(max_pages):
            try:
                resp = fetch_page(cursor, limit)
            except Exception as e:
                log.error("%s failed: %s", label, e)
                break

            if not resp.feed:
                log.debug("%s: empty page %d", label, page)
                break

            for item in resp.feed:
                post   = item.post
                raw_ts = post.indexed_at

                # indexed_at can be a datetime object or ISO string depending on SDK version
                if isinstance(raw_ts, datetime):
                    indexed_at = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
                else:
                    indexed_at = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))

                # Stop when we reach posts we already processed
                if since and indexed_at <= since:
                    log.debug("%s: reached already-seen posts, stopping", label)
                    return posts

                text = self._extract_text(post.record)
                if text:
                    # A quién responde este post (para el grafo de relaciones).
                    # Best-effort: item.reply.parent es un PostView con .author.
                    parent = getattr(getattr(item, "reply", None), "parent", None)
                    reply_to = getattr(getattr(parent, "author", None), "handle", None)
                    posts.append({
                        "handle"     : post.author.handle,
                        "text"       : text,
                        "uri"        : post.uri,
                        "indexed_at" : indexed_at.isoformat(),
                        "reply_to"   : reply_to,
                    })

            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break

        log.info("%s: total %d posts fetched", label, len(posts))
        return posts

    def get_list_feed(self, list_uri: str, since: datetime | None, limit: int = 50) -> list[dict]:
        """Posts de una **lista** de Bluesky (fuente tipo `list`)."""
        uri = self.resolve_list_uri(list_uri)
        return self._paginate_posts(
            lambda cursor, lim: self._client.app.bsky.feed.get_list_feed(
                {"list": uri, "limit": lim, **({"cursor": cursor} if cursor else {})}),
            f"get_list_feed[{uri}]", since, limit)

    def get_custom_feed(self, feed_uri: str, since: datetime | None, limit: int = 50) -> list[dict]:
        """Posts de un **feed generator** / algoritmo custom (fuente tipo `feed`)."""
        uri = self.resolve_list_uri(feed_uri)  # resuelve handle→DID si hace falta
        return self._paginate_posts(
            lambda cursor, lim: self._client.app.bsky.feed.get_feed(
                {"feed": uri, "limit": lim, **({"cursor": cursor} if cursor else {})}),
            f"get_custom_feed[{uri}]", since, limit)

    def get_list_members(self, list_uri: str) -> list[str]:
        """Handles de los miembros de una **lista** de Bluesky (para USER_GROUPS feed:)."""
        uri = self.resolve_list_uri(list_uri)
        handles: list[str] = []
        cursor = None
        for _ in range(10):  # cap: 10 páginas × 100 = 1000 miembros
            resp = self._client.app.bsky.graph.get_list(
                {"list": uri, "limit": 100, **({"cursor": cursor} if cursor else {})})
            handles += [item.subject.handle for item in (resp.items or [])]
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break
        return handles

    def get_follows(self) -> list[str]:
        """Handles de las cuentas que el bot sigue (para USER_GROUPS feed: type=following)."""
        handles: list[str] = []
        cursor = None
        for _ in range(10):
            resp = self._client.app.bsky.graph.get_follows(
                {"actor": self.handle, "limit": 100, **({"cursor": cursor} if cursor else {})})
            handles += [f.handle for f in (resp.follows or [])]
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break
        return handles

    def get_timeline(self, since: datetime | None, limit: int = 50) -> list[dict]:
        """Home **timeline** del bot: posts de las cuentas que sigue (fuente tipo `following`)."""
        return self._paginate_posts(
            lambda cursor, lim: self._client.app.bsky.feed.get_timeline(
                {"limit": lim, **({"cursor": cursor} if cursor else {})}),
            "get_timeline", since, limit)

    def get_feed_posts(self, source_type: str, identifier: str | None,
                       since: datetime | None, limit: int = 50) -> list[dict]:
        """Dispatcher de fuentes de feed (T7): `list` | `feed` | `following`.

        `identifier` es el at-URI de la lista/feed; se ignora para `following`.
        Tipo desconocido → cae a `list` con un warning (no rompe el loop).
        """
        if source_type == "following":
            return self.get_timeline(since, limit)
        if source_type == "feed":
            return self.get_custom_feed(identifier or "", since, limit)
        if source_type != "list":
            log.warning("get_feed_posts: tipo de fuente desconocido '%s' → uso 'list'", source_type)
        return self.get_list_feed(identifier or "", since, limit)

    def _external_embed(self, url: str):
        """Tarjeta de preview (embed external) para `url`: baja OG + sube la miniatura
        como blob. Best-effort: si algo falla, devuelve None (se postea sin tarjeta)."""
        card = _fetch_og_card(url)
        if not card:
            return None
        thumb = None
        if card.get("image"):
            try:
                req = urllib.request.Request(card["image"], headers={"User-Agent": _OG_UA})
                with urllib.request.urlopen(req, timeout=8) as r:
                    thumb = self._client.upload_blob(r.read(2_000_000)).blob
            except Exception as e:
                log.debug("thumb upload falló %s: %s", card["image"], e)
        external = models.AppBskyEmbedExternal.External(
            uri=url, title=(card["title"] or url)[:300], description=(card["description"] or "")[:1000],
            thumb=thumb,
        )
        return models.AppBskyEmbedExternal.Main(external=external)

    def _send_media(self, text: str, media_path: str, *, facets, reply_to=None):
        """Postea `media_path` como VIDEO (.mp4/.mov/...) o IMAGEN según la extensión.

        Los frames de TikTok resuelven a su .mp4 padre (ver _postable_media_path),
        así que acá puede llegar un video; se sube con send_video (la lib atproto
        maneja el upload al servicio de video de Bluesky). El alt = primeros 100 chars
        del texto.
        """
        data = Path(media_path).read_bytes()
        if Path(media_path).suffix.lower() in _VIDEO_EXTS:
            return self._client.send_video(
                text=text, video=data, video_alt=text[:100],
                reply_to=reply_to, facets=facets)
        return self._client.send_image(
            text=text, image=data, image_alt=text[:100],
            reply_to=reply_to, facets=facets)

    def reply(self, text: str, parent_uri: str, parent_cid: str,
              root_uri: str, root_cid: str, media_path: str | None = None) -> str:
        """
        Post a reply, optionally with an attached image OR video.
        parent = immediate post being replied to.
        root   = original post that started the thread.
        """
        text = text[:300]
        reply_to = {
            "root"  : {"uri": root_uri,   "cid": root_cid},
            "parent": {"uri": parent_uri, "cid": parent_cid},
        }
        facets = self._all_facets(text)
        if media_path:
            resp = self._send_media(text, media_path, facets=facets, reply_to=reply_to)
        else:
            # Tarjeta de preview del primer link (Bluesky admite un solo embed).
            urls = _find_urls_with_offsets(text)
            embed = self._external_embed(urls[0][0]) if urls else None
            resp = self._client.send_post(text=text, reply_to=reply_to, facets=facets, embed=embed)
        return resp.uri


    def post(self, text: str, limit: int = 295, media_path: str | None = None) -> str:
        """
        Post a standalone skeet (no reply), optionally with an attached image OR video.
        Returns the new post URI. Truncates at the last sentence-ending
        punctuation before `limit` to avoid cutting mid-word.
        """
        if len(text) > limit:
            cut = text[:limit]
            # Walk back to the last sentence boundary
            for i in range(len(cut) - 1, max(len(cut) - 60, 0), -1):
                if cut[i] in ".!?":
                    text = cut[: i + 1]
                    break
            else:
                # No punctuation found — cut at last space
                text = cut[: cut.rfind(" ")] if " " in cut else cut
        facets = self._all_facets(text)
        if media_path:
            resp = self._send_media(text, media_path, facets=facets)
        else:
            urls = _find_urls_with_offsets(text)
            embed = self._external_embed(urls[0][0]) if urls else None
            resp = self._client.send_post(text=text, facets=facets, embed=embed)
        return resp.uri

    def _extract_text(self, record) -> str:
        if hasattr(record, "text"):
            return record.text or ""
        if isinstance(record, dict):
            return record.get("text", "")
        return ""


# ---------------------------------------------------------------------------
# LLM: el cliente vive en router.py (ModelRouter + RoleLLM). Cada nodo recibe un
# RoleLLM ligado a su rol; el router resuelve endpoint/modelo y hace fallback.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Feed processor
# ---------------------------------------------------------------------------

class FeedProcessor:
    """
    Reads a Bluesky list feed, summarizes new posts with the LLM,
    and appends the summary to context/feeds/{feed_name}.md.

    Runs at most once per `interval_hours` per feed (tracked in SQLite).
    """

    # T23: prompt externalizado en prompts/feed_summary_prompt.md
    SUMMARIZE_SYSTEM = load_text(PROMPTS_DIR / "feed_summary_prompt.md")

    def __init__(self, bsky: BskyClient, router: ModelRouter, db: sqlite3.Connection):
        self.bsky   = bsky
        self.router = router
        self.db     = db

    def should_run(self, feed_name: str, interval_hours: int) -> bool:
        """Returns True if enough time has passed since the last run."""
        last_run = get_feed_last_run(self.db, feed_name)
        if last_run is None:
            return True
        last = datetime.fromisoformat(last_run)
        elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return elapsed_hours >= interval_hours

    def _summarize(self, posts: list[dict], feed_name: str) -> str | None:
        """Ask the LLM to summarize the posts. Returns None if nothing notable."""
        lines = [f"@{p['handle']}: {p['text']}" for p in posts[:50]]  # cap at 50 posts
        user  = f"Feed: {feed_name}\n\n" + "\n".join(lines)

        try:
            content = self.router.chat(
                "feed_summary",
                messages=[
                    {"role": "system", "content": f"{current_datetime_line()}\n\n{self.SUMMARIZE_SYSTEM}"},
                    {"role": "user",   "content": user},
                ],
                max_tokens=400,
            )
            result = (content or "").strip()
            return None if result.upper() == "NADA" else result
        except Exception as e:
            log.error("FeedProcessor: summarize failed for %s: %s", feed_name, e)
            return None

    def _append_to_file(self, feed_name: str, summary: str) -> None:
        """Append a timestamped summary entry to context/feeds/{feed_name}.md."""
        append_feed_summary(feed_name, summary)

    def process(self, feed_name: str, list_uri: str, interval_hours: int, full_backfill: bool = False) -> None:
        """
        Main entry point. Runs the full fetch → summarize → save pipeline.
        full_backfill=True ignores last_run and fetches as far back as the pagination cap allows.
        """
        if not self.should_run(feed_name, interval_hours):
            log.debug("Feed %s: skipping (not due yet)", feed_name)
            return

        log.info("Feed %s: fetching%s", feed_name, " (full backfill)" if full_backfill else "")

        # full_backfill → since=None fetches everything up to max_pages
        last_run_str = get_feed_last_run(self.db, feed_name)
        since        = None if full_backfill else (
            datetime.fromisoformat(last_run_str) if last_run_str else None
        )

        posts = self.bsky.get_list_feed(list_uri, since=since)
        log.info("Feed %s: %d new posts fetched", feed_name, len(posts))

        # Always update last_run, even if there's nothing to summarize
        save_feed_last_run(self.db, feed_name)

        if not posts:
            log.info("Feed %s: nothing new", feed_name)
            return

        summary = self._summarize(posts, feed_name)
        if summary:
            self._append_to_file(feed_name, summary)
        else:
            log.info("Feed %s: LLM found nothing notable", feed_name)

    def post_opinion(self, feed_name: str) -> None:
        """
        Read the latest summary block from context/feeds/{feed_name}.md,
        generate a short personal opinion using reflect_feed_prompt.md,
        and post it as a standalone skeet.
        """
        feed_path = FEEDS_DIR / f"{feed_name}.md"
        if not feed_path.exists():
            log.error("post_opinion: no feed file found for %s", feed_name)
            return

        # Extract the most recent summary block (last ## section)
        content = feed_path.read_text(encoding="utf-8")
        blocks  = content.split("\n## ")
        if len(blocks) < 2:
            log.error("post_opinion: feed file for %s has no summary entries yet", feed_name)
            return
        latest_block = blocks[-1].strip()

        prompt = load_text(PROMPTS_DIR / "reflect_feed_prompt.md")
        if not prompt:
            log.error("post_opinion: reflect_feed_prompt.md not found")
            return

        soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
        # Reinforce the character limit explicitly — models tend to ignore it otherwise
        system = (
            f"{soul}\n\n---\n{current_datetime_line()}\n\n---\n{prompt}\n\n"
            "IMPORTANTE: tu respuesta tiene que tener MENOS DE 250 CARACTERES en total. "
            "Contá los caracteres antes de responder. Si te pasás, cortá."
        )

        try:
            raw = self.router.chat(
                "feed_opinion",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": f"Feed: {feed_name}\n\n{latest_block}"},
                ],
                max_tokens=200,
            )
            if not raw:
                log.error("post_opinion: LLM returned empty content for %s", feed_name)
                return
            opinion = raw.strip()
        except Exception as e:
            log.error("post_opinion: LLM failed for %s: %s", feed_name, e)
            return

        log.info("post_opinion [%s]: %s", feed_name, opinion[:80])
        uri = self.bsky.post(opinion)
        log_bot_post(self.db, uri=uri, in_reply_to=None, reply_to_handle=None, text=opinion)
        log.info("post_opinion: posted %s", uri)


# ---------------------------------------------------------------------------
# T5 · Loop proactivo de feed (grafo langgraph)
#   fetch → summarize → reflect_decide (tool_calling, scope feed_reflection)
#         → post (si el agente decide postear y no es duplicado)
# El agente decide autónomamente si postea, según temática, mood y política.
# ---------------------------------------------------------------------------

# Guía de comportamiento por política de posteo (configurable por feed en settings.json).
def feed_policy_guidance(policy: str) -> str:
    """Guía de decisión de posteo por política (T23: prompt externalizado en
    prompts/feed_policy_{policy}.md). Cae a `balanced` si la política es desconocida."""
    return (load_text(PROMPTS_DIR / f"feed_policy_{policy}.md")
            or load_text(PROMPTS_DIR / "feed_policy_balanced.md"))


class FeedDecision(BaseModel):
    """Decisión del agente sobre si postear en el feed (structured output)."""
    should_post: bool = Field(
        default=False, description="True si vale la pena postear sobre este feed ahora."
    )
    reason: str = Field(default="", description="Motivo breve de la decisión (para logs).")
    text: str = Field(
        default="",
        validation_alias=AliasChoices("text", "message", "post"),
        description="El skeet a postear (<=250 chars, rioplatense). Vacío si should_post=False.",
    )
    image_query: str = Field(
        default="",
        description="Opcional: si una imagen del catálogo pega justo con el post, palabras "
                    "clave para buscarla (ej. 'gato sorprendido'). Vacío si no corresponde imagen.",
    )


# T6 · aprendizajes del feed → memoria/calendario (structured output)
class UserFactLearning(BaseModel):
    handle: str = Field(description="Handle del autor que reveló el hecho (sin @).")
    fact: str = Field(description="Hecho autorrevelado, frase corta en tercera persona.")


class EventLearning(BaseModel):
    title: str = Field(description="Título breve del evento.")
    event_at: str = Field(description="Fecha ISO 8601 (YYYY-MM-DD o YYYY-MM-DDTHH:MM).")
    handle: str | None = Field(default=None, description="Dueño del evento, o null si es de comunidad.")
    kind: str = Field(default="other", description="birthday|reminder|community|other")
    description: str | None = Field(default=None)


class FeedLearnings(BaseModel):
    facts: list[UserFactLearning] = Field(default_factory=list)
    events: list[EventLearning] = Field(default_factory=list)


class FeedState(TypedDict, total=False):
    # entrada
    feed_name: str
    feed_type: str
    list_uri: str
    interval_hours: int
    posting_policy: str
    force_post: bool
    full_backfill: bool
    learn: bool
    autonomous_images: bool
    # intermedio / salida
    posts: list
    posts_count: int
    summary: str | None
    learned_facts: int
    learned_events: int
    should_post: bool
    post_text: str
    image_query: str
    reason: str
    posted_uri: str


class FetchFeedNode:
    """Lee posts nuevos del feed dentro de la ventana temporal (desde last_run)."""

    def __init__(self, bsky: BskyClient, conn: sqlite3.Connection):
        self.bsky = bsky
        self.conn = conn

    def run(self, state: FeedState) -> dict:
        feed_name = state["feed_name"]
        interval  = state.get("interval_hours", 0)
        # Respeta la frecuencia salvo force o backfill.
        if not state.get("force_post") and not state.get("full_backfill") and interval:
            last_run = get_feed_last_run(self.conn, feed_name)
            if last_run is not None:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last_run)).total_seconds() / 3600
                if elapsed < interval:
                    log.info("Feed %s: no toca todavía (%.1fh < %dh)", feed_name, elapsed, interval)
                    return {"posts_count": 0}

        last_run_str = get_feed_last_run(self.conn, feed_name)
        since = None if state.get("full_backfill") else (
            datetime.fromisoformat(last_run_str) if last_run_str else None
        )
        posts = self.bsky.get_feed_posts(
            state.get("feed_type", "list"), state.get("list_uri"), since=since)
        save_feed_last_run(self.conn, feed_name)
        # Grafo de relaciones: autor ↔ respondido, mecánico y sin LLM. El gate
        # "ambos usuarios conocidos" vive en db.bump_relationship.
        for p in posts:
            if p.get("reply_to") and p["reply_to"] != BSKY_HANDLE:
                dbmod.bump_relationship(self.conn, p["handle"], p["reply_to"], kind="reply")
        log.info("Feed %s: %d posts nuevos", feed_name, len(posts))
        return {"posts": posts, "posts_count": len(posts)}


class SummarizeFeedNode:
    """Resume los posts (rol feed_summary) y appendea a la memoria del feed."""

    def __init__(self, router: ModelRouter, conn: sqlite3.Connection):
        self.router = router
        self.conn   = conn

    def run(self, state: FeedState) -> dict:
        posts = state.get("posts") or []
        if not posts:
            return {"summary": None}
        lines = [f"@{p['handle']}: {p['text']}" for p in posts[:50]]
        user  = f"Feed: {state['feed_name']}\n\n" + "\n".join(lines)
        try:
            content = self.router.chat(
                "feed_summary",
                messages=[
                    {"role": "system",
                     "content": f"{current_datetime_line()}\n\n{FeedProcessor.SUMMARIZE_SYSTEM}"},
                    {"role": "user", "content": user},
                ],
                max_tokens=400,
            )
        except Exception as e:
            log.error("SummarizeFeedNode: %s", e)
            return {"summary": None}
        summary = (content or "").strip()
        if not summary or summary.upper() == "NADA":
            return {"summary": None}
        append_feed_summary(state["feed_name"], summary)
        return {"summary": summary}


class LearnFromFeedNode:
    """
    T6: tras leer el feed, el agente extrae aprendizajes de los posts crudos.
    - Hechos autorrevelados por usuarios CON perfil → upsert_user_fact (dedup semántico).
    - Fechas/eventos → events (T4), con dedup idempotente (event_exists).
    Fechas relativas se resuelven con la fecha actual (T3). No afecta la decisión de postear.
    """

    def __init__(self, llm: RoleLLM, conn: sqlite3.Connection):
        self.llm  = llm
        self.conn = conn

    def run(self, state: FeedState) -> dict:
        if not state.get("learn", True):
            return {}
        posts = state.get("posts") or []
        if not posts:
            return {}

        lines = [f"[{p['handle']}]: {p['text']}" for p in posts[:50]]
        uri_by_handle = {p["handle"]: p.get("uri") for p in posts}
        prompt = load_text(PROMPTS_DIR / "feed_learnings_prompt.md")
        system = f"{current_datetime_line()}\n\n{prompt}"
        try:
            learnings = self.llm.complete(system, "\n".join(lines), FeedLearnings)
        except Exception as e:
            log.error("LearnFromFeedNode: %s", e)
            return {}

        n_facts = n_events = 0
        for f in learnings.facts:
            handle = f.handle.lstrip("@").strip()
            if handle and dbmod.user_exists(self.conn, handle):  # solo usuarios con perfil
                # upsert_user_fact devuelve None si es un duplicado semántico → no cuenta.
                if dbmod.upsert_user_fact(self.conn, handle, f.fact,
                                          source_uri=uri_by_handle.get(handle)) is not None:
                    n_facts += 1
        for ev in learnings.events:
            handle = ev.handle.lstrip("@").strip() if ev.handle else None
            if handle and not dbmod.user_exists(self.conn, handle):
                handle = None  # sin perfil → evento de comunidad (respeta el FK)
            if not dbmod.event_exists(self.conn, title=ev.title, event_at=ev.event_at, handle=handle):
                dbmod.create_event(self.conn, title=ev.title, event_at=ev.event_at,
                                   handle=handle, kind=ev.kind, description=ev.description, source="feed")
                n_events += 1

        if n_facts or n_events:
            log.info("Feed %s: aprendizajes → %d hechos, %d eventos",
                     state["feed_name"], n_facts, n_events)
        return {"learned_facts": n_facts, "learned_events": n_events}


class ReflectDecideNode:
    """
    Núcleo agéntico de T5. El agente lee el resumen y decide si postear, considerando
    temática, mood, política de posteo y sus posts recientes (para no repetirse). Puede
    llamar tools con scope feed_reflection (T1) antes de decidir.
    """

    def __init__(self, llm: RoleLLM, registry: ToolRegistry, conn: sqlite3.Connection):
        self.llm      = llm
        self.registry = registry
        self.conn     = conn

    def run(self, state: FeedState) -> dict:
        summary = state.get("summary")
        if not summary:
            return {"should_post": False, "reason": "sin resumen"}

        soul     = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
        reflect  = load_text(PROMPTS_DIR / "reflect_feed_prompt.md")
        policy   = state.get("posting_policy", "balanced")
        guidance = feed_policy_guidance(policy)
        recientes = recent_bot_posts(self.conn, limit=8)

        parts = [
            soul,
            f"\n---\n{current_datetime_line()}",
            mood_line(self.conn),
            memory_block(self.conn),
            prefs_block(self.conn),
            f"\n---\n{reflect}" if reflect else "",
            f"\n---\n{guidance}",
        ]
        # T26: skills scope feed_reflection (inline + índice on-demand)
        skills_block = skills_prompt_block(SKILLS_DIR, Scope.FEED_REFLECTION)
        if skills_block:
            parts.append(f"\n---\n{skills_block}")
        if recientes:
            parts.append(
                "\n---\nYa posteaste esto recientemente (NO lo repitas ni parafrasees):\n"
                + "\n".join(f"- {t}" for t in recientes)
            )
        if state.get("force_post"):
            parts.append(
                "\n---\nIMPORTANTE: esta corrida es FORZADA. Tenés que postear sí o sí: "
                "poné should_post=true y generá el mejor texto posible."
            )

        # Fase de tools (scope feed_reflection). Hoy puede no haber ninguna; queda cableado.
        tools = self.registry.openai_schemas(Scope.FEED_REFLECTION)
        if tools:
            tool_system = "\n".join(p for p in parts if p) + (
                "\n---\nSi necesitás info extra (buscar algo, ver el calendario, etc.) llamá "
                "a una tool. Si no, no llames ninguna."
            )
            try:
                _, tool_calls = self.llm.call_with_tools(tool_system, summary, tools)
                for call in tool_calls or []:
                    name  = call.function.name
                    cargs = json.loads(call.function.arguments)
                    outcome = self.registry.execute(name, cargs, ToolContext(state=state, conn=self.conn))
                    parts.append(f"\n---\nResultado de {name}: {outcome.text}")
            except Exception as e:
                log.warning("ReflectDecideNode: fase de tools falló: %s", e)

        # T12: posteo autónomo de imágenes (off por default). Solo se ofrece si el
        # feed lo habilita Y hay imágenes en el catálogo.
        images_on = bool(state.get("autonomous_images")) and \
            dbmod.get_image_catalog_stats(self.conn)["total"] > 0
        if images_on:
            parts.append(
                "\n---\nTenés un catálogo de imágenes. Si —y SOLO si— una imagen pega justo "
                "con tu post, poné en 'image_query' palabras clave para buscarla. Si no "
                "corresponde, dejá 'image_query' vacío (la mayoría de las veces va vacío)."
            )

        parts.append("\n---\n" + load_text(PROMPTS_DIR / "feed_decision_format.md"))
        system = "\n".join(p for p in parts if p)

        log_llm_context(f"feed_reflection ({state['feed_name']})", system, summary)
        try:
            decision = self.llm.complete(system, summary, FeedDecision)
        except Exception as e:
            log.error("ReflectDecideNode: %s", e)
            return {"should_post": False, "reason": f"error: {e}"}

        should = bool(decision.should_post) or bool(state.get("force_post"))
        image_query = decision.image_query if images_on else ""
        log.info("Feed %s: decisión should_post=%s (%s)%s",
                 state["feed_name"], should, decision.reason[:80],
                 f" +img={image_query!r}" if image_query else "")
        return {"should_post": should, "post_text": decision.text,
                "reason": decision.reason, "image_query": image_query}


_VALID_IMAGE_CATEGORIES = {"meme", "foto", "arte", "captura", "otro"}


_FRAME_RE = re.compile(r"_f\d+$")            # <id>_f<n>.jpg → frame de video (TikTok)
_MAX_VIDEO_BYTES = 50 * 1024 * 1024          # límite conservador de Bluesky (~50 MB / 60 s)
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}


def _is_video_frame(file_path: str) -> bool:
    """True si el archivo es un frame extraído de un video (ej. TikTok)."""
    return bool(_FRAME_RE.search(Path(file_path).stem))


def _frame_to_video_rel(frame_path: str) -> str:
    """De un frame <id>_f<n>.jpg al video padre <id>_0.mp4 (mismo dir, path relativo)."""
    p = Path(frame_path)
    base = _FRAME_RE.sub("", p.stem)
    return str(p.with_name(f"{base}_0.mp4"))


def _postable_media_path(file_path_rel: str) -> str | None:
    """Path ABSOLUTO posteable para una fila del catálogo, o None si no sirve.

    Un frame de video se postea como el VIDEO padre (.mp4), no como el fotograma
    suelto: así el bot sube el TikTok entero usando el frame como proxy de búsqueda.
    Si el video no está en disco o excede el límite de Bluesky → None (se prueba el
    siguiente candidato). El resto de las imágenes se postean tal cual.

    file_path se guarda relativo a la raíz del repo; devolvemos absoluto para que
    bsky.post/reply lo encuentre sin importar el cwd.
    """
    if _is_video_frame(file_path_rel):
        video = BASE_DIR / _frame_to_video_rel(file_path_rel)
        if video.exists() and video.stat().st_size <= _MAX_VIDEO_BYTES:
            return str(video)
        return None
    return str(BASE_DIR / file_path_rel)


def resolve_catalog_image(conn: sqlite3.Connection, query: str | None,
                          *, mark_used: bool = True) -> str | None:
    """Selecciona el mejor medio POSTEABLE del catálogo para `query`, o None.

    Devuelve un path absoluto que puede ser una IMAGEN o un VIDEO: los frames de
    video resuelven al .mp4 padre (ver _postable_media_path), así el bot postea el
    TikTok entero en vez de un fotograma suelto. **Guardrail (T12):** nunca devuelve
    algo sin descripción o con categoría inválida. Recorre los mejores candidatos y
    devuelve el primero posteable. Compartido por el reply y el loop proactivo.
    """
    if not query or not query.strip():
        return None
    results = dbmod.hybrid_search_image_catalog(conn, query.strip(), limit=8)
    for img in results:
        desc = (img.get("description") or "").strip()
        cat  = (img.get("category") or "").strip().lower()
        if not desc or cat not in _VALID_IMAGE_CATEGORIES:
            continue  # guardrail: no posteamos lo que no 'entendemos'
        path = _postable_media_path(img.get("file_path") or "")
        if not path:
            continue  # frame sin video utilizable → siguiente candidato
        if mark_used:
            dbmod.mark_image_used(conn, img["id"])
        log.info("%s elegido: %r → %s",
                 "video" if path.lower().endswith(".mp4") else "imagen", query, path)
        return path
    log.info("media: sin match posteable para %r", query)
    return None


class PostFeedNode:
    """Postea el skeet decidido, con guardia de duplicados contra bot_posts."""

    def __init__(self, bsky: BskyClient, conn: sqlite3.Connection):
        self.bsky = bsky
        self.conn = conn

    def run(self, state: FeedState) -> dict:
        if not state.get("should_post"):
            return {}
        text = (state.get("post_text") or "").strip()
        if not text:
            log.info("Feed %s: should_post pero sin texto — skip", state.get("feed_name"))
            return {}
        norm = _norm_text(text)
        if any(_norm_text(t) == norm for t in recent_bot_posts(self.conn, limit=20)):
            log.info("Feed %s: texto duplicado de un post reciente — skip", state.get("feed_name"))
            return {}
        # T12: imagen autónoma (opcional). El guardrail vive en resolve_catalog_image.
        image_path = None
        if state.get("autonomous_images") and state.get("image_query"):
            image_path = resolve_catalog_image(self.conn, state["image_query"])
        uri = self.bsky.post(text, media_path=image_path)
        log_bot_post(self.conn, uri=uri, in_reply_to=None, reply_to_handle=None, text=text)
        log.info("Feed %s: posteado %s%s", state.get("feed_name"), uri,
                 " (con imagen)" if image_path else "")
        return {"posted_uri": uri}


def _feed_has_posts(state: FeedState) -> str:
    return "learn" if state.get("posts_count", 0) > 0 else "END"


def _feed_has_summary(state: FeedState) -> str:
    return "reflect" if state.get("summary") else "END"


def _feed_should_post(state: FeedState) -> str:
    return "post" if state.get("should_post") else "END"


def build_feed_graph(router: ModelRouter, bsky: BskyClient, db: sqlite3.Connection,
                     registry: ToolRegistry):
    """
    Grafo proactivo de feed (T5 + T6):
        START → fetch → learn → summarize → reflect → post → END
                  ↓sin posts       ↓sin resumen   ↓no postea
                 END              END            END
    `learn` (T6) extrae hechos/eventos de los posts crudos; siempre sigue a summarize.
    """
    fetch     = FetchFeedNode(bsky, db)
    learn     = LearnFromFeedNode(RoleLLM(router, "update_profile"), db)
    summarize = SummarizeFeedNode(router, db)
    reflect   = ReflectDecideNode(RoleLLM(router, "feed_opinion"), registry, db)
    post      = PostFeedNode(bsky, db)

    g = StateGraph(FeedState)
    g.add_node("fetch",     fetch.run)
    g.add_node("learn",     learn.run)
    g.add_node("summarize", summarize.run)
    g.add_node("reflect",   reflect.run)
    g.add_node("post",      post.run)

    g.add_edge(START, "fetch")
    g.add_conditional_edges("fetch",     _feed_has_posts,   {"learn": "learn", "END": END})
    g.add_edge("learn", "summarize")
    g.add_conditional_edges("summarize", _feed_has_summary, {"reflect": "reflect", "END": END})
    g.add_conditional_edges("reflect",   _feed_should_post, {"post": "post", "END": END})
    g.add_edge("post", END)
    return g.compile()


def _run_feed_pass(graph, *, force_post: bool = False, full_backfill: bool = False,
                   respect_interval: bool = True) -> None:
    """Una pasada del grafo proactivo sobre todos los feeds habilitados.

    Reutilizado por `run_feed_loop` (CLI `--proactive`, one-shot, ignora el
    intervalo) y por el loop continuo `run()` (respeta el intervalo por feed).
    """
    for feed in FEEDS_CONFIG:
        if not feed.get("enabled", True):
            continue
        state: FeedState = {
            "feed_name":      feed["name"],
            "feed_type":      feed.get("type", "list"),
            "list_uri":       feed.get("uri"),
            "interval_hours": feed.get("interval_hours", 6) if respect_interval else 0,
            "posting_policy": feed.get("posting_policy", "balanced"),
            "learn":          feed.get("learn", True),
            "autonomous_images": feed.get("autonomous_images", False),
            "force_post":     force_post,
            "full_backfill":  full_backfill,
        }
        result = graph.invoke(state)
        if result.get("posted_uri"):
            log.info("Feed %s: OK → %s", feed["name"], result["posted_uri"])
        elif result.get("posts_count"):
            log.info("Feed %s: sin posteo (%s)", feed["name"], result.get("reason", "n/a"))


def run_feed_loop(force_post: bool = False, full_backfill: bool = False) -> None:
    """Loop proactivo (T5/T6) como pase único desde CLI (`--proactive`). Ignora intervalo."""
    db       = init_db()
    bsky     = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
    router   = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    registry = build_tool_registry(TOOLS_CONFIG, bsky=bsky, router=router, mcp_config=MCP_CONFIG)
    graph    = build_feed_graph(router, bsky, db, registry)
    _run_feed_pass(graph, force_post=force_post, full_backfill=full_backfill,
                   respect_interval=False)
    log.info("Loop proactivo completo.")


# ---------------------------------------------------------------------------
# T15 · Noticias / RSS gestionadas por admin
# ---------------------------------------------------------------------------
_NEWS_UA = "botata/1.0 (bot comunitario de Bluesky Argentina)"


def _strip_html(s: str | None) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def fetch_rss(url: str, max_items: int = 15) -> list[dict]:
    """Titulares + descripciones de un feed RSS 2.0 (stdlib xml, sin feedparser).
    Portado de maripobot_deprecated. Devuelve [{title, link, description, id}]."""
    req = urllib.request.Request(url, headers={"User-Agent": _NEWS_UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    root  = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):  # RSS 2.0 (channel/item)
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link") or "").strip()
        guid  = (item.findtext("guid") or "").strip()
        desc  = _strip_html(item.findtext("description"))
        ident = link or guid or title
        if not (title and ident):
            continue
        items.append({"title": title, "link": link or guid, "description": desc, "id": ident})
        if len(items) >= max_items:
            break
    return items


def _summarize_news(router: ModelRouter, items: list[dict], source: dict) -> str | None:
    """Comentario en la voz del bot sobre los titulares nuevos (rol feed_summary)."""
    soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
    prompt = load_text(PROMPTS_DIR / "summarize_news_prompt.md")
    label  = source.get("title") or source["host"]
    cat    = source.get("category")
    if cat:
        label = f"{label} · {cat}"  # el bot sabe la categoría de la fuente
    body   = "\n\n".join(f"[{label}] {it['title']}\n{it['description']}" for it in items[:15])
    try:
        content = router.chat(
            "feed_summary",
            messages=[
                {"role": "system", "content": f"{soul}\n\n{current_datetime_line()}\n\n{prompt}"},
                {"role": "user", "content": body},
            ],
            max_tokens=400,
        )
    except Exception as e:
        log.error("summarize_news: %s", e)
        return None
    return (content or "").strip() or None


def run_news_pass(bsky: BskyClient, router: ModelRouter, db: sqlite3.Connection, *,
                  force: bool = False, respect_interval: bool = True,
                  max_post_items: int = 3) -> None:
    """T15: por cada fuente RSS habilitada, postea lo NUEVO (dedup por item).
    `mode` por fuente: 'comment' (un comentario LLM de los items nuevos) o 'post'
    (cada item nuevo como skeet, capado). Master toggle NEWS_ENABLED (off por default)."""
    if not NEWS_ENABLED:
        return
    for src in NEWS_SOURCES:
        if not src.get("enabled", True):
            continue
        cursor   = "news:" + src["host"]
        interval = src.get("interval_hours", 6)
        if respect_interval and not force and interval:
            last = get_feed_last_run(db, cursor)
            if last:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last)).total_seconds() / 3600
                if elapsed < interval:
                    log.info("news %s: no toca todavía (%.1fh < %dh)", src["host"], elapsed, interval)
                    continue
        try:
            items = fetch_rss(src["url"])
        except Exception as e:
            log.error("news %s: fetch falló: %s", src["host"], e)
            continue
        save_feed_last_run(db, cursor)
        new = [it for it in items if not dbmod.news_item_posted(db, it["id"])]
        if not new:
            log.info("news %s: nada nuevo", src["host"])
            continue

        if src.get("mode", "comment") == "comment":
            comment = _summarize_news(router, new, src)
            if comment:
                uri = bsky.post(comment)
                log_bot_post(db, uri=uri, in_reply_to=None, reply_to_handle=None, text=comment)
                log.info("news %s: comentario posteado %s (%d items)", src["host"], uri, len(new))
            for it in new:  # marcados aunque el comentario falle: no reintentar los mismos
                dbmod.mark_news_item_posted(db, it["id"], src["host"], it["title"])
        else:  # mode == 'post'
            for it in new[:max_post_items]:
                text = f"{it['title']}\n{it['link']}" if it["link"] else it["title"]
                uri  = bsky.post(text)
                log_bot_post(db, uri=uri, in_reply_to=None, reply_to_handle=None, text=text)
                dbmod.mark_news_item_posted(db, it["id"], src["host"], it["title"])
                log.info("news %s: item posteado %s", src["host"], uri)


# ---------------------------------------------------------------------------
# Config por comandos de admin (T30)
# ---------------------------------------------------------------------------

# Lista viva de tareas del loop (la llena run()); las tools de config la mutan
# para que "prendé el heartbeat" aplique sin reiniciar.
_RUNTIME_TASKS: list[PeriodicTask] = []

# Claves que JAMÁS se cambian desde un post. Identidad (lock-out/secuestro) y
# endpoints/modelos (redirigir el tráfico LLM = exfiltración de prompts+memoria).
# Defensa en profundidad: aunque un handler futuro lo intente, el guard rechaza.
_PROTECTED_SETTINGS = ("BOT_HANDLE", "ADMIN_HANDLE", "ADMIN_HANDLES", "MODELS",
                       "OPENAI_ENDPOINT", "REASONING_MODEL", "LITE_MODEL", "IMAGE_MODEL",
                       "SPOTIFY_REDIRECT_URI", "USER_GROUPS")

# Las tools de config no se tocan a sí mismas (anti auto-lockout / escalación).
_CONFIG_TOOL_NAMES = frozenset({
    "get_bot_config", "set_tool_config", "set_task_config",
    "set_feed_config", "set_news_enabled", "set_mcp_enabled", "set_heartbeat",
})


def _fmt_interval(hours: float) -> str:
    if hours <= 0:
        return "en cada ciclo"
    if hours < 1:
        return f"cada {round(hours * 60)} minutos"
    return f"cada {hours:g} hora{'s' if hours != 1 else ''}"

# Regla: por comando solo se REDUCE exposición. Agregar estos scopes = solo UI.
_PUBLIC_SCOPES = frozenset({Scope.REPLY, Scope.FEED_REFLECTION})


def _delta_guard(before: dict, after: dict) -> list[str]:
    """Rechaza deltas que un comando jamás debe poder hacer (defensa en profundidad)."""
    for key in _PROTECTED_SETTINGS:
        if before.get(key) != after.get(key):
            return [f"cambio prohibido: {key} solo se toca desde la UI/archivos"]
    def _strip(cfg: dict) -> dict:
        return {k: v for k, v in (cfg or {}).items() if k != "enabled"}
    if ({n: _strip(c) for n, c in before.get("MCP", {}).items()} !=
            {n: _strip(c) for n, c in after.get("MCP", {}).items()}):
        return ["cambio prohibido: la estructura de los servers MCP solo se edita en la UI"]
    bt, at = before.get("TOOLS", {}), after.get("TOOLS", {})
    for name in _CONFIG_TOOL_NAMES:
        if bt.get(name) != at.get(name):
            return [f"cambio prohibido: la tool de configuración '{name}' no se toca por comando"]
    for name, cfg in at.items():
        added = set(cfg.get("scopes", [])) - set((bt.get(name) or {}).get("scopes", []))
        publica = added & _PUBLIC_SCOPES
        if publica:
            return [f"cambio prohibido: ampliar scopes públicos de '{name}' "
                    f"({sorted(publica)}) requiere la UI"]
        # Grupos: por comando solo se RESTRINGE. Con restricción vigente, quitarla
        # o sumar grupos = más gente puede usar la tool → solo UI.
        bg = set((bt.get(name) or {}).get("groups") or [])
        ag = set(cfg.get("groups") or [])
        if bg and (not ag or ag - bg):
            return [f"cambio prohibido: aflojar la restricción de grupos de '{name}' "
                    "(quitar la restricción o sumar grupos) requiere la UI"]
    return []


def _persist_settings_delta(apply: "Callable[[dict], None]") -> list[str]:
    """Lee settings.json fresco, aplica el delta, valida y escribe atómico (T22).

    Releer del disco evita pisar cambios hechos por la UI mientras el bot corre.
    Devuelve lista de errores ([] = ok). `_delta_guard` rechaza lo que un comando
    jamás debe poder hacer; los validadores de T22 chequean el resto.
    """
    from config_ui import ConfigStore  # lazy: evita el costo en el arranque normal
    store = ConfigStore(BASE_DIR)
    current = json.loads(store.settings_path.read_text(encoding="utf-8"))
    original = json.loads(json.dumps(current))
    apply(current)
    errs = _delta_guard(original, current)
    if errs:
        return errs
    return store.write_settings(current)


def _as_bool(value) -> bool | None:
    """Coerción tolerante para args del LLM ('true'/'false'/bool). None si no vino."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "sí", "si", "on")


def run_news_loop() -> None:
    """Pase único del pipeline de noticias desde CLI (`--news`). Ignora el intervalo."""
    db     = init_db()
    bsky   = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
    router = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    run_news_pass(bsky, router, db, force=True, respect_interval=False)
    log.info("News pass completo.")


# ---------------------------------------------------------------------------
# Heartbeat (T27): pase agéntico de calendario
# ---------------------------------------------------------------------------

def run_heartbeat_pass(bsky: "BskyClient", router: ModelRouter,
                       conn: sqlite3.Connection,
                       registry: ToolRegistry | None = None) -> None:
    """Mira los eventos de hoy/próximos + las instrucciones del heartbeat y
    DECIDE si vale la pena decir algo.

    Las instrucciones tienen dos capas: `prompts/heartbeat_checklist.md` es la
    checklist de base (versionada, editable a mano — el mecanismo PRINCIPAL) y
    `context/heartbeat_override.md` es el override runtime que setea el admin por
    comando (set_heartbeat — mecanismo AUXILIAR). El override, si existe y no
    está vacío, pisa a la checklist; borrarlo vuelve a la checklist.

    Misma filosofía que ReflectDecideNode: decisión del agente, sin cron rígido.
    Sin eventos NI instrucciones (ninguna de las dos capas) → return sin tocar
    el LLM (costo cero). El intervalo lo gatea el scheduler (cursor
    `task:heartbeat`), no esta función.
    """
    override = load_text(HEARTBEAT_OVERRIDE_PATH).strip()
    instrucciones = override or load_text(HEARTBEAT_CHECKLIST_PATH).strip()
    # Acciones agendadas (kind='bot_action') vencidas: ÓRDENES, no contexto — van
    # en su propia sección y se marcan done tras el pase (se ejecutan una vez).
    acciones = dbmod.due_bot_actions(conn)
    hoy      = [e for e in dbmod.events_today(conn) if e["kind"] != "bot_action"]
    proximos = [e for e in dbmod.upcoming_events(conn, limit=5)
                if e["id"] not in {h["id"] for h in hoy} and e["kind"] != "bot_action"]
    if not acciones and not hoy and not proximos and not instrucciones:
        log.info("heartbeat: sin eventos ni instrucciones — nada que decir")
        return

    def _fmt(e: dict) -> str:
        owner = f" (de @{e['handle']})" if e.get("handle") else " (comunidad)"
        return f"- {e['event_at']}: {e['title']}{owner}" + (
            f" — {e['description']}" if e.get("description") else "")

    soul = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
    parts = [soul, f"\n---\n{current_datetime_line()}", mood_line(conn),
             memory_block(conn), prefs_block(conn),
             f"\n---\n{load_text(PROMPTS_DIR / 'heartbeat_engine.md')}"]
    skills_block = skills_prompt_block(SKILLS_DIR, Scope.FEED_REFLECTION)
    if skills_block:
        parts.append(f"\n---\n{skills_block}")
    if override:
        parts.append("\n---\nINSTRUCCIONES VIGENTES del admin para este pase "
                     "(orden puntual que pisa tu tarea de base; tu tarea "
                     "principal ahora):\n" + override)
    elif instrucciones:
        parts.append("\n---\nINSTRUCCIONES POR DEFECTO del heartbeat "
                     "(tu tarea de base en cada pase):\n" + instrucciones)
    if acciones:
        parts.append(
            "\n---\nACCIONES AGENDADAS por el admin, vencidas AHORA (son órdenes: "
            "cumplilas en este pase, en tu voz de siempre; la regla de eventos "
            "personales NO aplica acá — esto te lo ordenaron explícitamente):\n"
            + "\n".join(f"- [{a['event_at']}] {a['title']}"
                        + (f" — {a['description']}" if a.get("description") else "")
                        for a in acciones))
    if hoy:
        parts.append("\n---\nEventos de HOY:\n" + "\n".join(_fmt(e) for e in hoy))
    if proximos:
        parts.append("\n---\nEventos próximos:\n" + "\n".join(_fmt(e) for e in proximos))
    recientes = recent_bot_posts(conn, limit=10)
    if recientes:
        parts.append("\n---\nYa posteaste esto recientemente (NO lo repitas):\n"
                     + "\n".join(f"- {t}" for t in recientes))
    # T12 en el heartbeat: las instrucciones ya son del admin → default true
    # (a diferencia de los feeds, que son opt-in). Toggle: TASKS.heartbeat.autonomous_images.
    images_on = bool(TASKS_CONFIG.get("heartbeat", {}).get("autonomous_images", True)) and \
        dbmod.get_image_catalog_stats(conn)["total"] > 0
    if images_on:
        parts.append(
            "\n---\nTenés un catálogo de imágenes. Si —y SOLO si— una imagen pega justo "
            "con tu post, poné en 'image_query' palabras clave para buscarla. Si no "
            "corresponde, dejá 'image_query' vacío (la mayoría de las veces va vacío)."
        )
    llm = RoleLLM(router, "feed_opinion")

    # Fase de tools (scope feed_reflection) — le da manos a las instrucciones:
    # "posteá un tema de Hermética" puede llamar search_music y traer un link
    # real; "qué se viene" puede mirar el calendario. Mismo patrón que
    # ReflectDecideNode; sin registry (tests/CLI viejo) se saltea.
    if registry is not None:
        hb_tools = registry.openai_schemas(Scope.FEED_REFLECTION)
        if hb_tools:
            tool_system = "\n".join(p for p in parts if p) + (
                "\n---\nSi tus instrucciones piden algo que requiere info real "
                "(buscar música, videos, noticias, calendario, web), llamá a la "
                "tool que corresponda ANTES de decidir. Si no hace falta, no "
                "llames ninguna."
            )
            try:
                _, tool_calls = llm.call_with_tools(
                    tool_system, "¿Hay algo que valga la pena decir hoy?", hb_tools)
                for call in tool_calls or []:
                    cname = call.function.name
                    cargs = json.loads(call.function.arguments)
                    outcome = registry.execute(cname, cargs, ToolContext(state={}, conn=conn))
                    log.info("heartbeat: tool %s(%s) → %r", cname, cargs, (outcome.text or "")[:120])
                    parts.append(f"\n---\nResultado de {cname}: {outcome.text}")
            except Exception as e:
                log.warning("heartbeat: fase de tools falló: %s", e)

    parts.append("\n---\n" + load_text(PROMPTS_DIR / "feed_decision_format.md"))
    system = "\n".join(p for p in parts if p)

    log_llm_context("heartbeat", system, "¿Hay algo que valga la pena decir hoy?")
    try:
        decision = llm.complete(system, "¿Hay algo que valga la pena decir hoy?", FeedDecision)
    except Exception as e:
        log.error("heartbeat: %s", e)
        return  # acciones NO marcadas done: error de LLM → reintentar el próximo pase
    # Acción considerada = acción cumplida (aunque el LLM decline con razón): una
    # orden vencida no se reintenta para siempre — el error de LLM es la única excepción.
    for a in acciones:
        dbmod.mark_event_done(conn, a["id"])
        if not decision.should_post:
            log.warning("heartbeat: acción agendada [%s] '%s' declinada (%s) — marcada done",
                        a["event_at"], a["title"], decision.reason[:80])
    log.info("heartbeat: should_post=%s (%s)", decision.should_post, decision.reason[:80])
    if not decision.should_post:
        return
    text = (decision.text or "").strip()
    if not text:
        log.info("heartbeat: should_post sin texto — skip")
        return
    norm = _norm_text(text)
    if any(_norm_text(t) == norm for t in recent_bot_posts(conn, limit=20)):
        log.info("heartbeat: texto duplicado de un post reciente — skip")
        return
    image_path = None
    if images_on and decision.image_query:
        image_path = resolve_catalog_image(conn, decision.image_query)
    uri = bsky.post(text, media_path=image_path)
    log_bot_post(conn, uri=uri, in_reply_to=None, reply_to_handle=None, text=text)
    log.info("heartbeat: posteado %s%s", uri, " (con imagen)" if image_path else "")


def run_heartbeat_loop() -> None:
    """Pase único de heartbeat desde CLI (`--heartbeat`). Ignora intervalo y toggle."""
    db     = init_db()
    bsky   = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
    router = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    registry = build_tool_registry(TOOLS_CONFIG, bsky=bsky, router=router, mcp_config=MCP_CONFIG)
    run_heartbeat_pass(bsky, router, db, registry)
    log.info("Heartbeat pass completo.")


# ---------------------------------------------------------------------------
# T6 (cierre) · Reflexión: destila LECCIONES conductuales de la actividad reciente
# ---------------------------------------------------------------------------
class LessonItem(BaseModel):
    lesson: str = Field(description="Lección conductual, una sola oración accionable.")
    about_handle: str | None = Field(
        default=None,
        description="Handle (sin @) si la lección es sobre un usuario puntual; null si es de comunidad.",
    )


class LessonsReflection(BaseModel):
    lessons: list[LessonItem] = Field(default_factory=list)


def run_reflection_pass(router: ModelRouter, conn: sqlite3.Connection, *,
                        activity_limit: int = 40, min_activity: int = 4) -> None:
    """Pase periódico INWARD-ONLY (nunca postea): mira la actividad reciente del
    bot y destila lecciones de comportamiento nuevas → db.lessons (upsert_lesson,
    con dedup semántico). Cierra el pendiente de T6 (facts+eventos ya se aprenden;
    faltaban las lecciones conductuales). Análogo a `maybe_update_lessons` del
    monolito, pero sobre bot_posts + la memoria de lecciones en SQLite.

    Con poca actividad (< min_activity) → return sin tocar el LLM (costo cero).
    """
    activity = recent_bot_activity(conn, limit=activity_limit)
    if len(activity) < min_activity:
        log.info("reflection: poca actividad (%d) — nada que destilar", len(activity))
        return

    existing = [r["lesson_text"] for r in conn.execute(
        "SELECT lesson_text FROM lessons ORDER BY id").fetchall()]
    existing_block = "\n".join(f"- {t}" for t in existing) or "Ninguna aún."

    soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
    prompt = load_text(PROMPTS_DIR / "reflect_lessons_prompt.md").format(
        existing_lessons=existing_block)
    system = f"{soul}\n---\n{current_datetime_line()}\n---\n{prompt}"

    def _fmt(a: dict) -> str:
        who = f"→ @{a['reply_to_handle']}" if a.get("reply_to_handle") else "(post suelto)"
        return f"- {a.get('posted_at', '')} {who}: {a.get('text', '')}"

    user = "Actividad reciente del bot (de más nuevo a más viejo):\n" + \
        "\n".join(_fmt(a) for a in activity)

    llm = RoleLLM(router, "update_profile")
    try:
        result = llm.complete(system, user, LessonsReflection)
    except Exception as e:
        log.error("reflection: %s", e)
        return

    n = 0
    for item in result.lessons:
        text = (item.lesson or "").strip()
        if not text:
            continue
        handle = (item.about_handle or "").lstrip("@").strip()
        scope  = f"user:{handle}" if handle and dbmod.user_exists(conn, handle) else "community"
        if dbmod.upsert_lesson(conn, text, scope=scope) is not None:
            n += 1
    log.info("reflection: %d lección(es) nueva(s) de %d posteos", n, len(activity))


def run_reflection_loop() -> None:
    """Pase único de reflexión desde CLI (`--reflect`). Ignora intervalo y toggle."""
    db     = init_db()
    router = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    run_reflection_pass(router, db)
    log.info("Reflection pass completo.")


# ---------------------------------------------------------------------------
# Estados de ánimo (moods)
# ---------------------------------------------------------------------------
# Un mood tiñe el tono del bot durante un día (más pila, más bajón, filoso…),
# transversal a replies + proactivo. Resolución en current_mood(); inyección en
# los system prompts outward vía mood_line(). Los moods viven en moods/*.md; el
# toggle/modo/schedule, en MOODS_CONFIG (settings.json). Ver moods/README.md.

# Índices de _WEEKDAY_KEYS == datetime.weekday() (Monday=0). Claves del schedule
# (en inglés: la config del core es portable, no atada al español).
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class MoodDecision(BaseModel):
    mood: str = Field(description="El name EXACTO de uno de los moods disponibles.")
    reason: str = Field(description="Por qué te sentís así hoy, en una frase.")


def _mood_state_get(conn: sqlite3.Connection) -> dict | None:
    """Lee el mood decidido (modo auto) de la tabla kv. None si no hay/roto."""
    raw = dbmod.kv_get(conn, "mood_state")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _default_mood():
    """El mood de base (MOODS.default) o None si no está configurado/no existe."""
    name = (MOODS_CONFIG.get("default") or "").strip()
    return moodmod.get_mood(MOODS_DIR, name) if name else None


def current_mood(conn: sqlite3.Connection):
    """El mood vigente HOY, o el default (MOODS.default) si nada más resuelve.

    - disabled            → None (comportamiento normal).
    - manual + fixed      → ese mood.
    - manual + schedule   → el del día de la semana (default si el día no está mapeado).
    - auto                → el guardado en kv para la fecha de hoy (lo escribe
                            run_mood_pass); default hasta que corra el pase del día.
    Sin MOODS.default configurado, el fallback sigue siendo None (sin mood).
    """
    cfg = MOODS_CONFIG
    if not cfg.get("enabled"):
        return None
    if str(cfg.get("mode", "manual")).lower() == "auto":
        st = _mood_state_get(conn)
        if st and st.get("date") == now_ar().date().isoformat() and st.get("mood"):
            return moodmod.get_mood(MOODS_DIR, st["mood"])
        return _default_mood()
    manual = cfg.get("manual", {}) or {}
    fixed = (manual.get("fixed") or "").strip()
    if fixed:
        return moodmod.get_mood(MOODS_DIR, fixed)
    schedule = manual.get("schedule", {}) or {}
    name = (schedule.get(_WEEKDAY_KEYS[now_ar().weekday()]) or "").strip()
    return moodmod.get_mood(MOODS_DIR, name) if name else _default_mood()


def mood_line(conn: sqlite3.Connection) -> str:
    """Sección de mood para inyectar en un system prompt outward. "" si no hay.
    Best-effort: jamás bloquea la generación."""
    try:
        mood = current_mood(conn)
    except Exception:
        log.debug("current_mood falló", exc_info=True)
        return ""
    return f"\n---\n{moodmod.mood_prompt_block(mood)}" if mood else ""


# ---------------------------------------------------------------------------
# Bloques de contexto desde la DB (memoria general, gustos, calendario)
# Best-effort: devuelven "" ante cualquier falla — jamás bloquean la generación.
# ---------------------------------------------------------------------------

def memory_block(conn: sqlite3.Connection) -> str:
    """Memoria general del bot (tabla bot_memory), completa. Reemplaza al viejo
    context/MEMORY.md: mismas entradas, pero con la DB como source of truth."""
    try:
        rows = dbmod.list_bot_memory(conn)
    except Exception:
        log.debug("memory_block falló", exc_info=True)
        return ""
    if not rows:
        return ""
    lines = "\n".join(f"- [{r['created_at'][:10]}] {r['text']}" for r in rows)
    return f"\n---\nTu memoria general (las fechas son de cuándo lo anotaste):\n{lines}"


def prefs_block(conn: sqlite3.Connection) -> str:
    """Gustos y disgustos del bot (tabla preferences)."""
    try:
        rows = dbmod.list_preferences(conn)
    except Exception:
        log.debug("prefs_block falló", exc_info=True)
        return ""
    likes    = [r["text"] for r in rows if r["kind"] == "like"]
    dislikes = [r["text"] for r in rows if r["kind"] == "dislike"]
    if not likes and not dislikes:
        return ""
    parts = ["\n---\nTus gustos y disgustos:"]
    if likes:
        parts.append("Te gusta:\n" + "\n".join(f"- {t}" for t in likes))
    if dislikes:
        parts.append("No te gusta:\n" + "\n".join(f"- {t}" for t in dislikes))
    return "\n".join(parts)


def calendar_block(conn: sqlite3.Connection, *, handle: str | None = None,
                   limit: int = 8) -> str:
    """Eventos de hoy y próximos, para que el bot tenga percepción temporal en
    cada reply (no depende de que el modelo llame get_upcoming_events). Excluye
    kind='bot_action': esas son órdenes del heartbeat, no contexto."""
    try:
        events = dbmod.upcoming_events(conn, limit=limit + 4, handle=handle)
    except Exception:
        log.debug("calendar_block falló", exc_info=True)
        return ""
    events = [e for e in events if e.get("kind") != "bot_action"][:limit]
    if not events:
        return ""
    def _fmt(e: dict) -> str:
        who  = f" (de @{e['handle']})" if e.get("handle") else ""
        desc = f" — {e['description']}" if e.get("description") else ""
        return f"- [{e['event_at']}] {e['title']}{who}{desc}"
    return ("\n---\nCalendario (hoy y próximos eventos agendados):\n"
            + "\n".join(_fmt(e) for e in events))


def run_mood_pass(router: ModelRouter, conn: sqlite3.Connection, *,
                  activity_limit: int = 10, climate_limit: int = 20,
                  force: bool = False) -> None:
    """Modo auto: elige el mood del día leyendo el CLIMA de la comunidad
    (interacciones recientes) + la ACTIVIDAD propia, y lo guarda en kv con el
    porqué. Reactivo: si lo vienen tratando mal, puede caer en bajón/arisco.

    Idempotente por día (si ya se decidió hoy no repite, salvo force=True). No-op
    si moods está apagado, no está en modo auto, o no hay moods disponibles."""
    cfg = MOODS_CONFIG
    if not cfg.get("enabled") or str(cfg.get("mode", "manual")).lower() != "auto":
        return
    index = moodmod.mood_index(MOODS_DIR)
    if not index:
        log.info("mood: no hay moods disponibles en %s", MOODS_DIR)
        return
    today = now_ar().date().isoformat()
    st = _mood_state_get(conn)
    if not force and st and st.get("date") == today and st.get("mood"):
        return  # ya decidido hoy

    climate  = dbmod.recent_interactions_all(conn, limit=climate_limit)
    activity = recent_bot_activity(conn, limit=activity_limit)

    options   = "\n".join(f"- {name}: {desc}" for name, desc in index)
    clima_txt = "\n".join(
        f"- [{c['created_at'][:10]}] @{c['handle']}: {c['summary']}" for c in climate
    ) or "Sin interacciones recientes."
    act_txt   = "\n".join(f"- {a.get('text', '')}" for a in activity) or "Sin actividad reciente."

    soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
    prompt = load_text(PROMPTS_DIR / "mood_decide_prompt.md").format(moods=options)
    system = f"{soul}\n---\n{current_datetime_line()}\n---\n{prompt}"
    user   = (f"Clima de la comunidad (interacciones recientes con vos):\n{clima_txt}\n\n"
              f"Tu actividad reciente:\n{act_txt}")

    try:
        decision = RoleLLM(router, "feed_opinion").complete(system, user, MoodDecision)
    except Exception as e:
        log.error("mood: %s", e)
        return
    chosen = moodmod.get_mood(MOODS_DIR, decision.mood)
    if not chosen:  # el modelo alucinó un name → fallback al primero disponible
        log.warning("mood: el modelo eligió %r (inexistente) — uso %r", decision.mood, index[0][0])
        chosen = moodmod.get_mood(MOODS_DIR, index[0][0])
    dbmod.kv_set(conn, "mood_state", json.dumps(
        {"date": today, "mood": chosen.name, "reason": decision.reason,
         "mode": "auto", "changed_at": now_ar().isoformat()}))
    log.info("mood: hoy el bot está %s — %s", chosen.name, decision.reason)


def run_mood_loop() -> None:
    """Pase único de mood desde CLI (`--mood`). Fuerza recalcular (force=True)."""
    db     = init_db()
    router = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    run_mood_pass(router, db, force=True)
    m = current_mood(db)
    log.info("Mood vigente: %s", m.name if m else "(ninguno)")


# ---------------------------------------------------------------------------
# Reflexión PÚBLICA (portado de maripobot: reflect_on_history/auto_reflect)
# ---------------------------------------------------------------------------
def run_public_reflection_pass(bsky: "BskyClient", router: ModelRouter,
                               conn: sqlite3.Connection, *,
                               activity_limit: int = 20, min_activity: int = 3) -> None:
    """Tarea periódica OUTWARD: el bot postea una reflexión en primera persona
    (≤300 chars, su voz) sobre lo que vivió — la pata pública de la reflexión
    (la inward, `run_reflection_pass`, destila lecciones a la DB).

    Material = lecciones recientes + actividad reciente del bot. El agente
    decide si hay algo con peso (FeedDecision; should_post=false es el
    resultado digno si no lo hay). Toggleable como toda tarea:
    TASKS.public_reflection (UI / set_task_config). Sin material → no llama
    al LLM (costo cero).
    """
    activity = recent_bot_activity(conn, limit=activity_limit)
    lessons = [r["lesson_text"] for r in conn.execute(
        "SELECT lesson_text FROM lessons ORDER BY id DESC LIMIT 8").fetchall()]
    if len(activity) < min_activity and not lessons:
        log.info("public_reflection: sin material — nada que reflexionar")
        return

    soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
    prompt = load_text(PROMPTS_DIR / "public_reflection_prompt.md")
    parts  = [soul, f"\n---\n{current_datetime_line()}", mood_line(conn),
              memory_block(conn), prefs_block(conn), f"\n---\n{prompt}"]
    if lessons:
        parts.append("\n---\nLecciones que destilaste últimamente:\n"
                     + "\n".join(f"- {t}" for t in lessons))
    recientes = recent_bot_posts(conn, limit=10)
    if recientes:
        parts.append("\n---\nTus posts recientes (NO te repitas — si ya "
                     "reflexionaste sobre esto, should_post=false):\n"
                     + "\n".join(f"- {t}" for t in recientes))
    parts.append("\n---\n" + load_text(PROMPTS_DIR / "feed_decision_format.md"))
    system = "\n".join(p for p in parts if p)

    def _fmt(a: dict) -> str:
        who = f"→ @{a['reply_to_handle']}" if a.get("reply_to_handle") else "(post suelto)"
        return f"- {a.get('posted_at', '')} {who}: {a.get('text', '')}"

    user = "Tu actividad reciente (de más nueva a más vieja):\n" + \
        "\n".join(_fmt(a) for a in activity)

    llm = RoleLLM(router, "feed_opinion")
    log_llm_context("public_reflection", system, user)
    try:
        decision = llm.complete(system, user, FeedDecision)
    except Exception as e:
        log.error("public_reflection: %s", e)
        return
    log.info("public_reflection: should_post=%s (%s)",
             decision.should_post, decision.reason[:80])
    if not decision.should_post:
        return
    text = (decision.text or "").strip()
    if not text:
        return
    norm = _norm_text(text)
    if any(_norm_text(t) == norm for t in recent_bot_posts(conn, limit=20)):
        log.info("public_reflection: duplicado de un post reciente — skip")
        return
    uri = bsky.post(text)
    log_bot_post(conn, uri=uri, in_reply_to=None, reply_to_handle=None, text=text)
    log.info("public_reflection: posteado %s", uri)


def run_public_reflection_loop() -> None:
    """Pase único desde CLI (`--reflect-public`). Ignora intervalo y toggle."""
    db     = init_db()
    bsky   = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
    router = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    run_public_reflection_pass(bsky, router, db)
    log.info("Public reflection pass completo.")


def run_playlist_share_pass(bsky: "BskyClient", router: ModelRouter,
                            conn: sqlite3.Connection) -> None:
    """Tarea periódica OUTWARD: postea un tema al azar de la playlist comunitaria
    (la misma que alimenta add_music_recommendation) con un comentario del LLM.

    Config TASKS.playlist_share: `comment` (default true) — apagado, postea solo
    título—artista+link, sin llamar al LLM (costo cero). Anti-repetición: evita
    tracks cuyo link ya aparece en los posts recientes del bot; si TODOS son
    recientes (playlist chica), elige igual al azar. Sin token de usuario o sin
    playlist → skip silencioso (el admin todavía no corrió spotify_auth.py).
    """
    import spotify_auth
    if not SPOTIFY_PLAYLIST_ID:
        log.info("playlist_share: sin SPOTIFY_PLAYLIST_ID — skip")
        return
    token = spotify_auth.user_token()
    if not token:
        log.info("playlist_share: sin token de usuario (spotify_auth.py) — skip")
        return
    try:
        tracks = [t for t in playlist_tracks(SPOTIFY_PLAYLIST_ID, token) if t.get("url")]
    except Exception as e:
        log.error("playlist_share: no pude leer la playlist: %s", e)
        return
    if not tracks:
        log.info("playlist_share: playlist vacía — skip")
        return

    recientes = recent_bot_posts(conn, limit=30)
    frescos = [t for t in tracks if not any(t["url"] in p for p in recientes)]
    track = random.choice(frescos or tracks)
    label = f"{track['title']} — {track['artist']}"

    comment_on = bool(TASKS_CONFIG.get("playlist_share", {}).get("comment", True))
    text = f"{label}\n{track['url']}"
    if comment_on:
        soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
        prompt = load_text(PROMPTS_DIR / "playlist_share_prompt.md")
        system = "\n".join(p for p in [
            soul, f"\n---\n{current_datetime_line()}", mood_line(conn), prefs_block(conn),
            f"\n---\n{prompt}",
            "\n---\n" + load_text(PROMPTS_DIR / "feed_decision_format.md")] if p)
        user = f"El tema que te tocó compartir: {label}\nLink: {track['url']}"
        try:
            decision = RoleLLM(router, "feed_opinion").complete(system, user, FeedDecision)
            if not decision.should_post:
                log.info("playlist_share: el agente declinó (%s)", decision.reason[:80])
                return
            comment = (decision.text or "").strip()
            if comment:
                text = comment if track["url"] in comment else f"{comment}\n{track['url']}"
        except Exception as e:
            # el comentario es opcional; el share es la función — degradar a sin comentario
            log.warning("playlist_share: LLM falló (%s) — posteo sin comentario", e)

    norm = _norm_text(text)
    if any(_norm_text(t) == norm for t in recientes):
        log.info("playlist_share: duplicado de un post reciente — skip")
        return
    uri = bsky.post(text)
    log_bot_post(conn, uri=uri, in_reply_to=None, reply_to_handle=None, text=text)
    log.info("playlist_share: posteado %s (%s)", uri, label)


def run_playlist_share_loop() -> None:
    """Pase único desde CLI (`--share-playlist`). Ignora intervalo y toggle."""
    db     = init_db()
    bsky   = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
    router = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    run_playlist_share_pass(bsky, router, db)
    log.info("Playlist share pass completo.")


# ---------------------------------------------------------------------------
# Tools concretas
# Los handlers reciben (args, ctx) y devuelven un ToolResult. ctx.state es el
# MentionState y ctx.conn la conexión sqlite. Se cierran sobre los globals del
# módulo (dbmod, MEMORY_PATH, load_text) para no acoplar tools.py a la app.
# El schema + scopes de cada tool se declara en build_tool_registry().
# ---------------------------------------------------------------------------

def _tool_save_to_user_profile(args: dict, ctx: ToolContext) -> ToolResult:
    content = args["content"]
    dbmod.upsert_user_fact(ctx.conn, ctx.state["author_handle"], content, source_uri="/remember")
    log.info("/remember → user_facts @%s: %s", ctx.state["author_handle"], content)
    return ToolResult(text=f"anotado en tu perfil: {content}")


def _tool_save_to_memory(args: dict, ctx: ToolContext) -> ToolResult:
    content = args["content"]
    who     = (ctx.state or {}).get("author_handle") or "?"
    mem_id  = dbmod.add_bot_memory(ctx.conn, content, source=f"tool:@{who}")
    if mem_id is None:
        return ToolResult(text=f"eso ya lo tenía anotado: {content}")
    log.info("/remember → bot_memory: %s", content)
    return ToolResult(text=f"anotado en mi memoria: {content}")


def _mood_susceptibility() -> float:
    """Susceptibilidad 0–1 de MOODS.susceptibility (default 0.5), clampeada."""
    try:
        s = float(MOODS_CONFIG.get("susceptibility", 0.5))
    except (TypeError, ValueError):
        s = 0.5
    return min(1.0, max(0.0, s))


def _mood_hysteresis_hours() -> float:
    try:
        h = float(MOODS_CONFIG.get("hysteresis_hours", 2))
    except (TypeError, ValueError):
        h = 2.0
    return max(0.0, h)


class _MoodMatch(BaseModel):
    mood: str  # name exacto de la lista, o "" si ninguno se parece


# Aproximador semántico para choose_mood ('tierno' → chill). Lo setea
# build_tool_registry cuando hay router; sin él (tests) no se aproxima.
# LLM lite y no embeddings a propósito: bge-m3 sobre palabras sueltas
# cross-idioma es ruido puro (medido 2026-07-24: 'tierno' → snarky).
_MOOD_MATCHER: "Callable[[str], str | None] | None" = None


def _make_mood_matcher(router: "ModelRouter") -> "Callable[[str], str | None]":
    def match(name: str) -> str | None:
        index = moodmod.mood_index(MOODS_DIR)
        if not index:
            return None
        options = "\n".join(f"- {n}: {d}" for n, d in index)
        try:
            result = RoleLLM(router, "classify").complete(
                "Sos un clasificador. El bot quiso ponerse en un estado de ánimo que no "
                "existe con ese nombre. Elegí el mood MÁS CERCANO en significado de la "
                "lista (devolvé su name exacto), o mood=\"\" si ninguno se parece razonablemente.",
                f"Estado de ánimo pedido: '{name}'\n\nMoods disponibles:\n{options}",
                _MoodMatch,
            )
            matched = (result.mood or "").strip().lower()
            return matched if any(matched == n for n, _ in index) else None
        except Exception as e:
            log.warning("mood: aproximación LLM falló: %s", e)
            return None
    return match


def _tool_choose_mood(args: dict, ctx: ToolContext) -> ToolResult:
    """Cambia el mood VIGENTE (estado persistido, tiñe todo lo que sigue) — no
    un tono efímero de esta respuesta. El admin bypassea susceptibilidad e
    histéresis; el bot solo puede en modo auto."""
    name   = (args.get("mood") or "").strip().lower()
    reason = (args.get("reason") or "").strip() or "sin razón declarada"
    admin  = is_admin_handle((ctx.state or {}).get("author_handle"))
    cfg    = MOODS_CONFIG
    if not cfg.get("enabled"):
        return ToolResult(text="los moods están apagados (MOODS.enabled=false); no hay mood que cambiar.")
    if str(cfg.get("mode", "manual")).lower() != "auto":
        return ToolResult(text="el mood está en modo manual (lo fija el admin por config); no puedo cambiarlo solo.")
    if not admin and _mood_susceptibility() <= 0:
        return ToolResult(text="con susceptibility=0 mi humor no se mueve; sigo como estaba.")
    st = _mood_state_get(ctx.conn) or {}
    if not admin and st.get("changed_at"):
        try:
            elapsed_h = (now_ar() - datetime.fromisoformat(st["changed_at"])).total_seconds() / 3600
            if elapsed_h < _mood_hysteresis_hours():
                return ToolResult(text=f"cambié de humor hace poco (estoy {st.get('mood')}); "
                                       "todavía no me muevo de ahí.")
        except (TypeError, ValueError):
            pass
    if name in ("reset", "default"):
        dbmod.kv_del(ctx.conn, "mood_state")
        base = _default_mood()
        log.info("mood: reset %s → %s (%s)", st.get("mood") or "(ninguno)",
                 base.name if base else "(sin default)", "admin" if admin else "reactivo")
        if base:
            return ToolResult(text=f"listo, humor reiniciado: vuelvo a mi base ({base.name}).")
        return ToolResult(text="listo, humor reiniciado (sin mood hasta el próximo pase diario).")
    chosen, approx = moodmod.get_mood(MOODS_DIR, name), ""
    if not chosen and _MOOD_MATCHER:
        matched = _MOOD_MATCHER(name)
        if matched:
            chosen = moodmod.get_mood(MOODS_DIR, matched)
            approx = f" — tomé '{name}' como {matched}, lo más parecido que tengo"
    if not chosen:
        options = ", ".join(n for n, _ in moodmod.mood_index(MOODS_DIR))
        return ToolResult(text=f"no conozco el mood '{name}' ni encontré uno parecido. Disponibles: {options}")
    dbmod.kv_set(ctx.conn, "mood_state", json.dumps({
        "date": now_ar().date().isoformat(), "mood": chosen.name, "reason": reason,
        "mode": "admin" if admin else "reactive", "changed_at": now_ar().isoformat()}))
    log.info("mood: cambio %s → %s (%s) — %s",
             st.get("mood") or "(ninguno)", chosen.name, "admin" if admin else "reactivo", reason)
    return ToolResult(text=f"listo: ahora estoy {chosen.name} ({reason}){approx}.")


def _tool_add_preference(args: dict, ctx: ToolContext) -> ToolResult:
    kind  = (args.get("kind") or "").strip().lower()
    text  = (args.get("text") or "").strip()
    admin = is_admin_handle((ctx.state or {}).get("author_handle"))
    if kind not in ("like", "dislike"):
        return ToolResult(text="kind inválido: usá 'like' o 'dislike'.")
    if not text:
        return ToolResult(text="falta el texto de la preferencia.")
    if not admin and PREFS_MODE == "manual":
        return ToolResult(text="mis gustos están en modo manual: solo el admin los toca.")
    pid = dbmod.add_preference(ctx.conn, kind, text, source="admin" if admin else "bot")
    if pid is None:
        return ToolResult(text=f"eso ya estaba entre mis preferencias: {text}")
    verbo = "me gusta" if kind == "like" else "no me gusta"
    log.info("preferences: +%s %r (source=%s)", kind, text, "admin" if admin else "bot")
    return ToolResult(text=f"anotado: {verbo} {text}.")


def _tool_remove_preference(args: dict, ctx: ToolContext) -> ToolResult:
    text  = (args.get("text") or "").strip()
    admin = is_admin_handle((ctx.state or {}).get("author_handle"))
    if not admin and PREFS_MODE != "full_auto":
        return ToolResult(text="no puedo sacar gustos en este modo (PREFS.mode); solo el admin puede.")
    pref = dbmod.find_preference(ctx.conn, text)
    if pref is None:
        return ToolResult(text=f"no tenía anotado '{text}' entre mis preferencias.")
    if not admin and pref.get("source") == "admin":
        return ToolResult(text=f"'{pref['text']}' lo definió el admin — no lo puedo sacar yo.")
    dbmod.delete_preference(ctx.conn, pref["id"])
    log.info("preferences: -%s %r", pref["kind"], pref["text"])
    return ToolResult(text=f"listo, saqué '{pref['text']}' de mis preferencias.")


def _tool_get_debug_info(args: dict, ctx: ToolContext) -> ToolResult:
    s = ctx.state
    return ToolResult(text=(
        f"[debug] mode={s['mode']} | author={s['author_handle']} | text={s['mention_text'][:80]}"
    ))


def _tool_get_help(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(text=(
        "comandos: /remember <texto>, /bloques (quién te bloquea, vía ClearSky), "
        "/check-role (tu rol y permisos), /imagen <búsqueda>, /debug, /help"
    ))


def _tool_use_skill(args: dict, ctx: ToolContext) -> ToolResult:
    """T26: carga el cuerpo de una skill on-demand (índice en el system prompt)."""
    name = (args.get("name") or "").strip()
    body = get_skill_body(SKILLS_DIR, name)
    if body is None:
        return ToolResult(text=f"[skill desconocida o deshabilitada: {name}]")
    return ToolResult(text=body)


def _tool_search_images(args: dict, ctx: ToolContext) -> ToolResult:
    query    = args.get("query", "")
    category = args.get("category")
    results  = dbmod.hybrid_search_image_catalog(ctx.conn, query, category=category, limit=8)
    # Primer candidato con path posteable (frame → video padre; ver _postable_media_path).
    best, best_path = None, None
    for r in results:
        p = _postable_media_path(r.get("file_path") or "")
        if p:
            best, best_path = r, p
            break
    if not best:
        return ToolResult(text=f"no encontré imágenes para '{query}'")
    dbmod.mark_image_used(ctx.conn, best["id"])
    summary = ", ".join(f"{r['description'][:50]}… [{r['category']}]" for r in results[:3])
    return ToolResult(text=f"encontré {len(results)} imágenes: {summary}", image_path=best_path)


# ─── T9 · Calendar: leer/escribir la tabla events (T4) como tools ────────────
def _format_events(events: list[dict]) -> str:
    if not events:
        return "no hay eventos agendados"
    lines = []
    for e in events:
        when  = e["event_at"][:16].replace("T", " ")
        owner = "comunidad" if e.get("handle") is None else f"@{e['handle']}"
        desc  = f" — {e['description']}" if e.get("description") else ""
        lines.append(f"[{e['id']}] {when} · {e['title']} ({owner}){desc}")
    return "\n".join(lines)


def _tool_get_upcoming_events(args: dict, ctx: ToolContext) -> ToolResult:
    """Lee la agenda. Un usuario ve sus eventos + los de comunidad; el admin y el
    loop proactivo (sin author) ven todos. Incluye los de hoy aunque ya hayan pasado."""
    author = ctx.state.get("author_handle")
    scope_handle = None if (not author or is_admin_handle(author)) else author
    limit  = int(args.get("limit") or 10)
    today  = dbmod.events_today(ctx.conn, handle=scope_handle)
    up     = dbmod.upcoming_events(ctx.conn, handle=scope_handle, limit=limit)
    seen: set[int] = set()
    merged = [e for e in (*today, *up) if not (e["id"] in seen or seen.add(e["id"]))]
    merged.sort(key=lambda e: e["event_at"])
    return ToolResult(text=_format_events(merged))


def _tool_create_event(args: dict, ctx: ToolContext) -> ToolResult:
    """Agenda un evento. Regla de propiedad: el admin puede crear para la comunidad
    (handle omitido) o para cualquier usuario; un usuario común SIEMPRE crea para sí
    mismo (nunca para otro). Idempotente por (día + dueño + título)."""
    author   = ctx.state.get("author_handle")
    is_admin = is_admin_handle(author)
    title    = (args.get("title") or "").strip()
    event_at = (args.get("event_at") or "").strip()
    if not title or len(event_at) < 10:
        return ToolResult(text="necesito un título y una fecha (YYYY-MM-DD) para agendar")
    if is_admin:
        req   = (args.get("handle") or "").lstrip("@").strip()
        owner = req or None  # None = evento de comunidad
    else:
        owner = author       # usuario común: siempre para sí mismo, ignora 'handle'
    if owner is not None and not dbmod.user_exists(ctx.conn, owner):
        return ToolResult(text=f"@{owner} no tiene perfil todavía, no puedo agendarle un evento")
    if dbmod.event_exists(ctx.conn, title=title, event_at=event_at, handle=owner):
        return ToolResult(text=f"ese evento ya estaba agendado: {title}")
    kind = (args.get("kind") or "other").strip() or "other"
    downgraded = False
    if kind == "bot_action":
        # Acción agendada = orden para que el BOT postee algo a una hora. Permiso
        # parametrizable (BOT_ACTIONS_FROM), default solo admin: superficie de
        # prompt injection si cualquiera le agenda contenido a un bot público.
        if not is_admin and BOT_ACTIONS_FROM != "any":
            kind, downgraded = "reminder", True
        else:
            owner = None  # las acciones son del bot, no del usuario que las pidió
    desc = (args.get("description") or "").strip() or None
    src  = f"tool:@{author}" if author else "tool"
    eid  = dbmod.create_event(ctx.conn, title=title, event_at=event_at, handle=owner,
                              description=desc, kind=kind, source=src)
    who  = "la comunidad" if owner is None else f"@{owner}"
    when = event_at[:16].replace("T", " ")
    if downgraded:
        return ToolResult(
            text=f"agendé '{title}' el {when} como recordatorio tuyo (id {eid}) — "
                 "ordenarme posteos solo puede el admin, pero lo voy a tener presente")
    if kind == "bot_action":
        return ToolResult(text=f"dale, acción agendada: '{title}' el {when} (id {eid}) — "
                               "la ejecuto en el primer pase después de esa hora")
    return ToolResult(text=f"listo, agendé '{title}' para {who} el {when} (id {eid})")


def _tool_get_my_recent_posts(args: dict, ctx: ToolContext) -> ToolResult:
    """T25: el bot consulta su propia actividad reciente (lo que posteó/respondió)."""
    limit    = int(args.get("limit") or 8)
    reply_to = (args.get("handle") or "").lstrip("@").strip() or None
    rows = recent_bot_activity(ctx.conn, limit=limit, reply_to=reply_to)
    if not rows:
        if reply_to:
            return ToolResult(text=f"no encontré posts míos respondiéndole a @{reply_to}")
        return ToolResult(text="no tengo posts recientes registrados")
    lines = []
    for r in rows:
        when = (r.get("posted_at") or "")[:16].replace("T", " ")
        tgt  = f" (respuesta a @{r['reply_to_handle']})" if r.get("reply_to_handle") else ""
        lines.append(f"{when}: {r['text']}{tgt}")
    return ToolResult(text="\n".join(lines))


_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _brave_search(query: str, count: int = 5) -> list[dict]:
    """GET a Brave Search (stdlib, sin deps nuevas). Devuelve [{title, url, description}].
    Levanta en error de red/HTTP (el handler lo maneja)."""
    qs  = urllib.parse.urlencode({"q": query, "count": count})
    req = urllib.request.Request(
        f"{_BRAVE_ENDPOINT}?{qs}",
        headers={"X-Subscription-Token": BRAVE_API_KEY or "", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = (data.get("web") or {}).get("results") or []
    strip = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()  # Brave marca términos con <strong>
    return [
        {"title": strip(r.get("title")), "url": r.get("url", ""),
         "description": strip(r.get("description"))}
        for r in results[:count]
    ]


def _tool_web_search(args: dict, ctx: ToolContext) -> ToolResult:
    """T8: búsqueda web vía Brave. Degradación graceful si falta la key o falla la API."""
    if not BRAVE_API_KEY:
        return ToolResult(text="la búsqueda web no está configurada")
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(text="necesito algo para buscar")
    count = max(1, min(int(args.get("count") or 5), 10))
    try:
        results = _brave_search(query, count)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return ToolResult(text="estoy limitado para buscar ahora, probá en un rato")
        log.error("web_search: HTTP %s para '%s'", e.code, query)
        return ToolResult(text="no pude buscar ahora")
    except Exception as e:
        log.error("web_search: falló '%s': %s", query, e)
        return ToolResult(text="no pude buscar ahora")
    if not results:
        return ToolResult(text=f"no encontré resultados para '{query}'")
    lines = [f"- {r['title']}: {r['description'].strip()} ({r['url']})" for r in results]
    return ToolResult(text="\n".join(lines))


# ─── T13 · Música (Spotify, Client Credentials — headless, sin OAuth ni deps) ──
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_API       = "https://api.spotify.com/v1"
_spotify_token_cache: dict = {"value": None, "exp": 0.0}


def _spotify_token() -> str | None:
    """Access token vía Client Credentials (app-only, playlists públicas). Cacheado."""
    cid    = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    now = time.time()
    if _spotify_token_cache["value"] and now < _spotify_token_cache["exp"]:
        return _spotify_token_cache["value"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req  = urllib.request.Request(
        _SPOTIFY_TOKEN_URL, data=data,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        tok = json.loads(resp.read().decode("utf-8"))
    _spotify_token_cache["value"] = tok["access_token"]
    _spotify_token_cache["exp"]   = now + tok.get("expires_in", 3600) - 60
    return tok["access_token"]


def _spotify_get(path: str, token: str, params: dict | None = None) -> dict:
    url = f"{_SPOTIFY_API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_spotify_tracks(query: str, limit: int = 5, market: str = "AR") -> list[dict] | None:
    """Busca temas en Spotify (endpoint /search, app-only Client Credentials — headless,
    sin OAuth). Devuelve [{title, artist, album, url}] o None si no hay credenciales.

    Se usa /search en vez del endpoint de playlists porque Spotify bloquea (403) la
    lectura de tracks de playlist con Client Credentials; /search sí está permitido.
    """
    token = _spotify_token()
    if not token:
        return None
    data  = _spotify_get("/search", token,
                        {"q": query, "type": "track", "limit": limit, "market": market})
    items = (data.get("tracks") or {}).get("items") or []
    return [
        {
            "id":     t.get("id"),
            "title":  t["name"],
            "artist": ", ".join(a["name"] for a in t["artists"]),
            "album":  t["album"]["name"],
            "url":    t["external_urls"]["spotify"],
        }
        for t in items
    ]


def _tool_search_music(args: dict, ctx: ToolContext) -> ToolResult:
    """T13: busca temas en Spotify por consulta (canción/artista/vibe) y devuelve
    título+artista+link. La opinión la compone el LLM del nodo (reply/feed)."""
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(text="necesito una canción, artista o vibe para buscar")
    if not (os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET")):
        return ToolResult(text="la música no está configurada")
    try:
        tracks = search_spotify_tracks(query, limit=5)
    except Exception as e:
        log.error("search_music: %s", e)
        return ToolResult(text="no pude buscar música ahora")
    if not tracks:
        return ToolResult(text=f"no encontré temas para '{query}'")
    lines = [f"- {t['title']} — {t['artist']} ({t['url']})" for t in tracks]
    return ToolResult(text="\n".join(lines))


# ─── Playlist de recomendaciones (token de USUARIO — spotify_auth.py) ─────────

def _spotify_post(path: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{_SPOTIFY_API}{path}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def playlist_tracks(playlist_id: str, token: str) -> list[dict]:
    """Tracks de la playlist: [{id, title, artist, url}]. Paginado, cap 1000.

    Endpoint `/items` (migración Spotify feb-2026: `/tracks` fue REMOVIDO para
    apps en Development Mode y el wrapper de la respuesta pasó de `track` a
    `item`). Solo funciona sobre playlists propias del usuario autorizado.
    """
    out: list[dict] = []
    offset = 0
    for _ in range(10):
        data = _spotify_get(f"/playlists/{playlist_id}/items", token,
                            {"fields": "items(item(id,name,artists(name),external_urls)),total",
                             "limit": 100, "offset": offset})
        items = data.get("items") or []
        for it in items:
            t = it.get("item") or it.get("track")  # tolerante al wrapper viejo
            if not t or not t.get("id"):
                continue
            out.append({
                "id":     t["id"],
                "title":  t.get("name"),
                "artist": ", ".join(a.get("name", "") for a in (t.get("artists") or [])),
                "url":    (t.get("external_urls") or {}).get("spotify"),
            })
        offset += len(items)
        if not items or offset >= int(data.get("total") or 0):
            break
    return out


def playlist_track_ids(playlist_id: str, token: str) -> set[str]:
    """IDs de los tracks ya en la playlist (para dedup de recomendaciones)."""
    return {t["id"] for t in playlist_tracks(playlist_id, token)}


def _norm_track_key(title: str | None, artist: str | None) -> tuple[str, str]:
    """Clave de dedup por canción, no por edición: Spotify tiene el mismo tema con
    IDs distintos por álbum/remaster. Baja a minúsculas, corta el sufijo de edición
    ("Tema - Remastered 2019" → "tema") y se queda con el artista principal."""
    t = (title or "").casefold().strip().split(" - ")[0].strip()
    a = (artist or "").casefold().split(",")[0].strip()
    return (t, a)


def add_track_to_playlist(query: str) -> dict:
    """Busca `query` y agrega el mejor match a la playlist comunitaria.

    Devuelve {status: added|duplicate|not_found|unavailable, track?}. `unavailable`
    = falta config o autorización de usuario (spotify_auth.py). Excepciones de red
    las maneja el caller.
    """
    import spotify_auth
    if not SPOTIFY_PLAYLIST_ID:
        return {"status": "unavailable", "reason": "sin SPOTIFY_PLAYLIST_ID en settings"}
    token = spotify_auth.user_token()
    if not token:
        return {"status": "unavailable",
                "reason": "sin token de usuario (correr python src/spotify_auth.py)"}
    tracks = search_spotify_tracks(query, limit=1)
    if not tracks or not tracks[0].get("id"):
        return {"status": "not_found"}
    track = tracks[0]
    # Dedup doble: por ID y por canción (título+artista normalizados) — el mismo
    # tema existe en Spotify con IDs distintos según la edición/remaster.
    key = _norm_track_key(track["title"], track["artist"])
    existentes = playlist_tracks(SPOTIFY_PLAYLIST_ID, token)
    if any(e["id"] == track["id"] or _norm_track_key(e["title"], e["artist"]) == key
           for e in existentes):
        return {"status": "duplicate", "track": track}
    _spotify_post(f"/playlists/{SPOTIFY_PLAYLIST_ID}/items", token,
                  {"uris": [f"spotify:track:{track['id']}"]})
    return {"status": "added", "track": track}


def _tool_add_music(args: dict, ctx: ToolContext) -> ToolResult:
    """Tool `add_music_recommendation`: recomendación de un power_user → la playlist."""
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(text="necesito saber qué canción o artista querés recomendar")
    try:
        out = add_track_to_playlist(query)
    except Exception as e:
        log.error("add_music_recommendation: %s", e)
        return ToolResult(text="no pude tocar la playlist ahora, probá más tarde")
    track = out.get("track") or {}
    label = f"{track.get('title')} — {track.get('artist')}"
    if out["status"] == "added":
        return ToolResult(text=f"listo, agregué {label} a la playlist ({track.get('url')})")
    if out["status"] == "duplicate":
        return ToolResult(text=f"{label} ya estaba en la playlist ({track.get('url')})")
    if out["status"] == "not_found":
        return ToolResult(text=f"no encontré '{query}' en Spotify, probá con título y artista")
    log.warning("add_music_recommendation no disponible: %s", out.get("reason"))
    return ToolResult(text="la playlist de recomendaciones no está configurada todavía")


# ─── T14 · Video (YouTube Data API v3, stdlib — headless) ──────────────────────
_YOUTUBE_API = "https://www.googleapis.com/youtube/v3"


def _youtube_get(path: str, params: dict) -> dict:
    """GET a la YouTube Data API (stdlib). Requiere YOUTUBE_API_KEY en el entorno."""
    key = os.environ.get("YOUTUBE_API_KEY")
    url = f"{_YOUTUBE_API}/{path}?" + urllib.parse.urlencode({**params, "key": key})
    with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def youtube_top_videos(query: str | None = None, region: str = "AR",
                       limit: int = 10) -> list[dict] | None:
    """Videos vía YouTube Data API. Con `query` → search; sin query → mostPopular de la
    región. Devuelve [{title, url, channel}] o None si falta YOUTUBE_API_KEY."""
    if not os.environ.get("YOUTUBE_API_KEY"):
        return None
    if query:
        data = _youtube_get("search", {"part": "snippet", "q": query, "type": "video",
                                       "maxResults": limit, "regionCode": region})
        out = []
        for it in data.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            sn  = it.get("snippet") or {}
            if vid:
                out.append({"title": sn.get("title", ""),
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "channel": sn.get("channelTitle", "")})
        return out
    data = _youtube_get("videos", {"part": "snippet", "chart": "mostPopular",
                                   "regionCode": region, "maxResults": limit})
    return [
        {"title": (it.get("snippet") or {}).get("title", ""),
         "url": f"https://www.youtube.com/watch?v={it['id']}",
         "channel": (it.get("snippet") or {}).get("channelTitle", "")}
        for it in data.get("items", [])
    ]


def fetch_top_video(conn: sqlite3.Connection | None = None,
                    query: str | None = None) -> dict | None:
    """Trae un video (mostPopular AR o búsqueda por `query`) que el bot no haya compartido
    todavía (dedup contra bot_posts). None si no hay key o nada nuevo."""
    videos = youtube_top_videos(query)
    if not videos:
        return None
    ya_posteados = " ".join(recent_bot_posts(conn, limit=30)) if conn is not None else ""
    for v in videos:
        if v["url"] not in ya_posteados:
            return v
    return None


def get_youtube_transcript(video_url: str, languages: tuple[str, ...] = ("es", "en")) -> str | None:
    """Transcript de un video de YouTube (best-effort). Portado de maripobot_deprecated:
    import lazy de `youtube_transcript_api` (dep opcional) + proxy Webshare si hay
    credenciales. Si la dep no está o falla (bans de IP, sin transcript), devuelve None."""
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", video_url)
    if not m:
        return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        log.debug("youtube_transcript_api no instalado — sin transcript")
        return None
    try:
        wu, wp = os.environ.get("WEBSHARE_USER"), os.environ.get("WEBSHARE_PASSWORD")
        if wu and wp:
            from youtube_transcript_api.proxies import WebshareProxyConfig
            api = YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(proxy_username=wu, proxy_password=wp))
        else:
            api = YouTubeTranscriptApi()
        transcript = api.fetch(m.group(1), languages=list(languages))
        return " ".join(e.text for e in transcript)
    except Exception as e:
        log.debug("youtube transcript falló para %s: %s", video_url, e)
        return None


def _tool_share_video(args: dict, ctx: ToolContext) -> ToolResult:
    """T14: trae un video de YouTube (mostPopular AR, o búsqueda si viene `query`) +
    transcript best-effort. La opinión la compone el LLM del nodo (reply/feed)."""
    if not os.environ.get("YOUTUBE_API_KEY"):
        return ToolResult(text="los videos no están configurados")
    query = (args.get("query") or "").strip() or None
    try:
        video = fetch_top_video(ctx.conn, query)
    except Exception as e:
        log.error("share_video: %s", e)
        return ToolResult(text="no pude traer un video ahora")
    if not video:
        return ToolResult(text="no encontré un video para compartir ahora")
    text = f"video: {video['title']} — {video['url']} (canal: {video['channel']})"
    transcript = get_youtube_transcript(video["url"])
    if transcript:
        text += f"\ntranscript (para tu comentario, recortado): {transcript[:1500]}"
    return ToolResult(text=text)


def _tool_get_news(args: dict, ctx: ToolContext) -> ToolResult:
    """T15 (reply): un usuario pide noticias/links de las fuentes RSS configuradas,
    opcionalmente filtradas por categoría. Independiente de NEWS_ENABLED (que gobierna
    solo el posteo autónomo)."""
    if not NEWS_SOURCES:
        return ToolResult(text="no tengo fuentes de noticias configuradas")
    cat     = (args.get("category") or "").strip().lower() or None
    sources = [s for s in NEWS_SOURCES if s.get("enabled", True)]
    if cat:
        sources = [s for s in sources
                   if cat in (s.get("category", "") + " " + s.get("title", "")).lower()]
    if not sources:
        return ToolResult(text=f"no tengo fuentes de noticias de '{cat}'" if cat
                          else "no hay fuentes de noticias activas")
    lines: list[str] = []
    for s in sources:
        try:
            items = fetch_rss(s["url"], max_items=3)
        except Exception as e:
            log.error("get_news %s: %s", s["host"], e)
            continue
        label = s.get("title") or s["host"]
        for it in items[:3]:
            link = f" {it['link']}" if it.get("link") else ""
            lines.append(f"- [{label}] {it['title']}{link}")
    if not lines:
        return ToolResult(text="no pude traer noticias ahora")
    return ToolResult(text="\n".join(lines[:8]))


def _tool_reset_my_memory(args: dict, ctx: ToolContext) -> ToolResult:
    """T11 `resetme`: borra SOLO la memoria del usuario que lo pide (hechos +
    embeddings + eventos propios). Nunca toca datos de otros; no bloquea."""
    handle = ctx.state.get("author_handle")
    if not handle:
        return ToolResult(text="no pude identificar tu cuenta para borrar tu memoria")
    counts = dbmod.purge_user_memory(ctx.conn, handle)
    log.info("resetme: memoria purgada de @%s (%s)", handle, counts)
    return ToolResult(text=(
        f"listo, borré lo que sabía de vos: {counts['facts']} hechos y "
        f"{counts['events']} eventos tuyos. arrancamos de cero."
    ))


def _make_block_me_tool(bsky: "BskyClient | None") -> ToolHandler:
    """T10 `blockme`: bloquea al usuario que lo pide y borra TODA su memoria
    (facts + embeddings + events + relationships + perfil). Se cierra sobre bsky."""
    def _block_me(args: dict, ctx: ToolContext) -> ToolResult:
        handle = ctx.state.get("author_handle")
        if not handle:
            return ToolResult(text="no pude identificar tu cuenta para bloquearte")
        # Bloquear primero: si falla, NO borramos nada (evita dejar al usuario sin
        # memoria pero interactuando).
        if bsky is None or not bsky.block_user(handle):
            return ToolResult(text="no pude bloquearte ahora, probá de nuevo en un rato (no borré nada)")
        counts = dbmod.purge_user_memory(
            ctx.conn, handle, include_relationships=True, drop_profile=True)
        log.info("blockme: @%s bloqueado + memoria purgada %s", handle, counts)
        return ToolResult(text=(
            f"listo, te bloqueé y borré todo lo que sabía de vos "
            f"({counts['facts']} hechos, {counts['events']} eventos). chau."
        ))
    return _block_me


def _make_summarize_feed_tool(bsky: "BskyClient | None", router: "ModelRouter | None") -> ToolHandler:
    """Handler de `summarize_feed`: resume en vivo los posts recientes de un feed.
    Se cierra sobre bsky+router (no viven en ToolContext). Scope REPLY, toggleable."""
    def _summarize_feed(args: dict, ctx: ToolContext) -> ToolResult:
        if bsky is None or router is None:
            return ToolResult(text="[summarize_feed no disponible en este contexto]")
        name = (args.get("feed_name") or "").strip()
        enabled = [f for f in FEEDS_CONFIG if f.get("enabled", True)]
        if not enabled:
            return ToolResult(text="no hay feeds configurados para resumir")
        feed = next((f for f in enabled if f["name"] == name), None) if name else enabled[0]
        if feed is None:
            return ToolResult(text=f"no conozco un feed llamado '{name}'")
        since = datetime.now(timezone.utc) - timedelta(hours=6)
        try:
            posts = bsky.get_feed_posts(
                feed.get("type", "list"), feed.get("uri"), since=since, limit=50)
        except Exception as e:
            log.error("summarize_feed: fetch falló: %s", e)
            return ToolResult(text="no pude leer el feed ahora, probá más tarde")
        if not posts:
            return ToolResult(text="el feed no tuvo movimiento en las últimas horas")
        lines = [f"@{p['handle']}: {p['text']}" for p in posts[:50]]
        # Prompt propio (on-demand): a diferencia del loop proactivo, acá el usuario
        # pidió el resumen → siempre devolvemos algo útil, sin la escotilla "NADA".
        prompt = load_text(PROMPTS_DIR / "summarize_feed_tool_prompt.md")
        try:
            content = router.chat(
                "feed_summary",
                messages=[
                    {"role": "system", "content": f"{current_datetime_line()}\n\n{prompt}"},
                    {"role": "user", "content": f"Feed: {feed['name']}\n\n" + "\n".join(lines)},
                ],
                max_tokens=400,
            )
        except Exception as e:
            log.error("summarize_feed: LLM falló: %s", e)
            return ToolResult(text="no pude resumir el feed ahora")
        summary = (content or "").strip()
        if not summary or summary.upper() == "NADA":
            return ToolResult(text="el feed está tranquilo, no hay mucho movimiento ahora mismo")
        return ToolResult(text=summary)
    return _summarize_feed


# Membresía dinámica de grupos vía feeds ("feed:polcifeed" en USER_GROUPS).
# TTL: la lista se re-consulta como mucho cada 15 min; entre medio, cache.
_GROUP_FEED_TTL_S = 900


def _make_group_feed_resolver(bsky: "BskyClient") -> "Callable[[str], frozenset[str]]":
    """Resuelve "feed:<name>" → handles miembros del feed, con cache TTL y stale-ok.

    Solo feeds con membresía definida: type `list` (miembros de la lista) y
    `following` (a quién sigue el bot). Un feed algorítmico (type `feed`) no
    tiene miembros → vacío con warning. Error de red → se sirve el último
    resultado bueno (un hiccup no le saca permisos a nadie); sin cache previo
    → vacío (cerrado).
    """
    cache: dict[str, tuple[float, frozenset[str]]] = {}

    def resolve(name: str) -> frozenset[str]:
        now = time.monotonic()
        hit = cache.get(name)
        if hit and now < hit[0]:
            return hit[1]
        feed = next((f for f in FEEDS_CONFIG if f.get("name") == name), None)
        if feed is None:
            log.warning("USER_GROUPS: 'feed:%s' no matchea ningún feed de FEEDS", name)
            return frozenset()
        ftype = feed.get("type", "list")
        try:
            if ftype == "list":
                members = frozenset(bsky.get_list_members(feed["uri"]))
            elif ftype == "following":
                members = frozenset(bsky.get_follows())
            else:
                log.warning("USER_GROUPS: 'feed:%s' es type=%s — sin membresía definida", name, ftype)
                return frozenset()
        except Exception as e:
            log.warning("USER_GROUPS: no pude resolver miembros de 'feed:%s': %s%s",
                        name, e, " — uso el cache anterior" if hit else "")
            if hit:  # stale-ok: reintento corto, sirviendo lo último bueno
                cache[name] = (now + 60, hit[1])
                return hit[1]
            return frozenset()
        cache[name] = (now + _GROUP_FEED_TTL_S, members)
        log.info("USER_GROUPS: 'feed:%s' → %d miembros", name, len(members))
        return members

    return resolve


def build_tool_registry(config: dict | None = None, *,
                        bsky: "BskyClient | None" = None,
                        router: "ModelRouter | None" = None,
                        mcp_config: dict | None = None) -> ToolRegistry:
    """Registra las tools concretas y aplica overrides de settings.json → sección TOOLS.

    Las tools de memoria/imágenes son scope ADMIN. `summarize_feed` es scope REPLY
    (usuarios) y se cierra sobre bsky+router para resumir el feed en vivo; se
    activa/desactiva por config como cualquier otra tool.

    `mcp_config` (settings.json → MCP, T29): conecta MCP servers externos y
    registra sus tools ANTES de `apply_config`, así la sección TOOLS también
    las puede overridear. Import lazy: sin config MCP el SDK ni se carga.
    """
    reg = ToolRegistry()
    reg.register(
        "save_to_user_profile",
        "Save a fact about the user who is speaking to their profile file. "
        "Use for personal data: age, pronouns, location, job, teams, preferences, events.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to save, written as a short plain sentence."}
            },
            "required": ["content"],
        },
        _tool_save_to_user_profile,
        {Scope.ADMIN},
    )
    reg.register(
        "save_to_memory",
        "Save a general fact to the bot's own memory (always loaded into context). "
        "Use for community context, standing instructions for the bot, or world-facts.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to save, written as a short plain sentence."}
            },
            "required": ["content"],
        },
        _tool_save_to_memory,
        {Scope.ADMIN},
    )

    # choose_mood: solo se le ofrece al BOT (scopes reply/feed_reflection) si los
    # moods están en auto con susceptibility > 0 — la config requiere reinicio,
    # igual que toda la sección MOODS, así que decidir los scopes acá es correcto.
    # El admin la tiene siempre (control desde Bluesky, bypassea los guards).
    s = _mood_susceptibility()
    mood_scopes = {Scope.ADMIN}
    if (MOODS_CONFIG.get("enabled")
            and str(MOODS_CONFIG.get("mode", "manual")).lower() == "auto" and s > 0):
        mood_scopes |= {Scope.REPLY, Scope.FEED_REFLECTION}
    if s >= 0.7:
        mood_hint = ("Cambiá de humor cuando la conversación te mueva: si te alegran, "
                     "te hacen reír, te insultan o te conmueven, reaccioná.")
    elif s >= 0.3:
        mood_hint = ("Cambiá de humor solo si algo te afecta de verdad (te levantan el "
                     "ánimo cuando estabas mal, te maltratan seguido) — no por cualquier cosa.")
    else:
        mood_hint = ("Cambiá de humor SOLO ante algo muy fuerte (agresión seria, gesto "
                     "excepcional). Casi siempre la respuesta correcta es NO usar esta tool.")
    # El enum previene el bug de moods alucinados ('tierno') en origen: el modelo
    # elige de la lista real. Red de seguridad si igual manda otra cosa (endpoint
    # que ignora el enum): _MOOD_MATCHER aproxima con el rol classify.
    global _MOOD_MATCHER
    if router is not None:
        _MOOD_MATCHER = _make_mood_matcher(router)
    mood_names = [n for n, _ in moodmod.mood_index(MOODS_DIR)]
    mood_param: dict = {"type": "string",
                        "description": "Uno de tus moods disponibles (elegí el más cercano a lo que "
                                       "sentís), o 'reset' para volver a tu humor de base."}
    if mood_names:
        mood_param["enum"] = mood_names + ["reset"]
    reg.register(
        "choose_mood",
        "Cambia tu estado de ánimo VIGENTE (persiste y tiñe todo lo que sigas "
        "posteando, no solo esta respuesta). " + mood_hint,
        {
            "type": "object",
            "properties": {
                "mood":   mood_param,
                "reason": {"type": "string", "description": "Por qué cambiás de humor, en una frase."},
            },
            "required": ["mood", "reason"],
        },
        _tool_choose_mood,
        mood_scopes,
    )

    # Preferencias (gustos/disgustos): el admin siempre puede; el bot según
    # PREFS.mode (manual = ni se le ofrecen, add_only = solo agregar,
    # full_auto = agregar y sacar — nunca las source='admin').
    add_pref_scopes = {Scope.ADMIN}
    rm_pref_scopes  = {Scope.ADMIN}
    if PREFS_MODE in ("add_only", "full_auto"):
        add_pref_scopes |= {Scope.REPLY, Scope.FEED_REFLECTION}
    if PREFS_MODE == "full_auto":
        rm_pref_scopes |= {Scope.REPLY, Scope.FEED_REFLECTION}
    reg.register(
        "add_preference",
        "Anotá un gusto (kind='like') o disgusto (kind='dislike') TUYO, del bot — "
        "algo que descubriste que te gusta o no. Es identidad duradera, no una "
        "opinión pasajera: usala solo cuando algo realmente se vuelve parte tuya.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "'like' o 'dislike'."},
                "text": {"type": "string", "description": "La preferencia, corta y concreta (ej. 'los panchos', 'que me traten de usted')."},
            },
            "required": ["kind", "text"],
        },
        _tool_add_preference,
        add_pref_scopes,
    )
    reg.register(
        "remove_preference",
        "Sacá un gusto o disgusto tuyo que ya no va (aparecen en tu system prompt). "
        "Los que definió el admin no se pueden sacar.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "El texto EXACTO de la preferencia a sacar."},
            },
            "required": ["text"],
        },
        _tool_remove_preference,
        rm_pref_scopes,
    )
    reg.register(
        "get_debug_info",
        "Return current runtime debug information about the bot.",
        {"type": "object", "properties": {}, "required": []},
        _tool_get_debug_info,
        {Scope.ADMIN},
    )
    reg.register(
        "get_help",
        "Return the list of available commands.",
        {"type": "object", "properties": {}, "required": []},
        _tool_get_help,
        {Scope.ADMIN},
    )
    reg.register(
        "search_images",
        "Search the image catalog for images matching a query. "
        "Use when the admin asks for a meme, image, foto, or picture. "
        "Returns the path to the best matching image so it can be posted.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query in Spanish, e.g. 'meme de gato', 'perro enojado'."},
                "category": {"type": "string", "description": "Optional category filter: 'meme', 'foto', 'arte', 'captura', 'otro'."},
            },
            "required": ["query"],
        },
        _tool_search_images,
        {Scope.ADMIN},
    )
    reg.register(
        "summarize_feed",
        "Resume los posts recientes (últimas horas) del feed de la comunidad. "
        "Usala cuando un usuario pregunta qué se está hablando en el feed, pide un "
        "resumen del feed/timeline, o quiere saber la actividad reciente de la comunidad.",
        {
            "type": "object",
            "properties": {
                "feed_name": {"type": "string",
                              "description": "Nombre del feed a resumir. Omitir para el feed principal."}
            },
            "required": [],
        },
        _make_summarize_feed_tool(bsky, router),
        {Scope.REPLY},
    )
    reg.register(
        "get_upcoming_events",
        "Consulta la agenda de la comunidad: eventos de hoy y próximos (cumpleaños, "
        "juntadas, recordatorios). Usala cuando un usuario pregunta qué se viene / qué hay "
        "agendado, o para decidir si saludar/recordar algo en el loop proactivo.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Cuántos eventos próximos traer (default 10)."}
            },
            "required": [],
        },
        _tool_get_upcoming_events,
        {Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN},
    )
    reg.register(
        "create_event",
        "Agenda un evento en el calendario (fecha + título). El admin puede agendar para "
        "la comunidad (omitiendo 'handle') o para un usuario; un usuario común solo puede "
        "agendar para sí mismo. Si te piden que VOS postees algo a una hora ('posteá el "
        "himno a las 00:00'), usá kind='bot_action' con la instrucción en 'description'.",
        {
            "type": "object",
            "properties": {
                "title":       {"type": "string", "description": "Título breve del evento."},
                "event_at":    {"type": "string", "description": "Fecha ISO 8601: YYYY-MM-DD o YYYY-MM-DDTHH:MM. Resolvé fechas relativas ('mañana', 'el sábado') usando la fecha de HOY."},
                "description": {"type": "string", "description": "Detalle opcional del evento. Para 'bot_action': la instrucción de QUÉ postear."},
                "kind":        {"type": "string", "description": "Tipo: 'birthday', 'meetup', 'reminder', 'other', o 'bot_action' (orden para que el bot postee algo a esa hora; solo la cumple si viene del admin)."},
                "handle":      {"type": "string", "description": "Solo admin: dueño del evento (sin @), u omitir para evento de comunidad. Ignorado para usuarios comunes (su evento es siempre propio)."},
            },
            "required": ["title", "event_at"],
        },
        _tool_create_event,
        {Scope.ADMIN},
    )
    reg.register(
        "get_my_recent_posts",
        "Consulta lo que VOS (el bot) venís posteando y respondiendo últimamente. Usala "
        "cuando un usuario pregunta qué estuviste diciendo, qué posteaste, de qué hablaste, "
        "o qué le respondiste a alguien. Opcional: 'handle' para filtrar a quién le respondiste.",
        {
            "type": "object",
            "properties": {
                "limit":  {"type": "integer", "description": "Cuántos posts traer (default 8)."},
                "handle": {"type": "string", "description": "Opcional: solo posts que respondieron a este handle (sin @)."},
            },
            "required": [],
        },
        _tool_get_my_recent_posts,
        {Scope.REPLY, Scope.ADMIN},
    )
    reg.register(
        "reset_my_memory",
        "Borra ÚNICAMENTE la memoria del usuario que te habla: sus hechos, embeddings y "
        "eventos propios. Llamala SOLO cuando el usuario pide EXPLÍCITAMENTE borrar/resetear "
        "su memoria (ej. '/resetme', 'borrá todo lo que sabés de mí', 'olvidate de mí'). "
        "Nunca borra datos de otras personas y no bloquea a nadie.",
        {"type": "object", "properties": {}, "required": []},
        _tool_reset_my_memory,
        {Scope.REPLY, Scope.ADMIN},
    )
    reg.register(
        "block_me",
        "Bloquea al usuario que te habla Y borra TODA su memoria (hechos, embeddings, "
        "eventos, relaciones y perfil). Acción destructiva e irreversible. Llamala SOLO "
        "cuando el usuario pide EXPLÍCITAMENTE que lo bloquees (ej. '/blockme', "
        "'bloqueame y olvidate de mí'). Nunca bloquea ni borra a terceros.",
        {"type": "object", "properties": {}, "required": []},
        _make_block_me_tool(bsky),
        {Scope.REPLY, Scope.ADMIN},
    )
    reg.register(
        "web_search",
        "Busca información actual en la web (Brave Search). Usala cuando el usuario pide "
        "buscar algo, pregunta por datos actuales/recientes, o necesitás info que no está "
        "en tu memoria. Devuelve los primeros resultados con título, resumen y link.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Qué buscar, en lenguaje natural."},
                "count": {"type": "integer", "description": "Cuántos resultados (default 5, máx 10)."},
            },
            "required": ["query"],
        },
        _tool_web_search,
        {Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN},
    )
    reg.register(
        "search_music",
        "Busca canciones en Spotify por consulta (título, artista o vibe) y devuelve los "
        "primeros resultados con título, artista y link. Usala cuando un usuario pide música, "
        "una canción o un artista, o cuando el agente quiere compartir un tema en el feed.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Qué buscar: canción, artista, o un vibe (ej. 'rock nacional melancólico')."}
            },
            "required": ["query"],
        },
        _tool_search_music,
        {Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN},
    )
    reg.register(
        "add_music_recommendation",
        "Agrega una recomendación musical de un usuario a la playlist comunitaria de Spotify. "
        "Usala cuando un usuario te recomienda un tema/canción o te pide agregar música a la "
        "lista/playlist (ej. 'agregá X a la lista', 'te recomiendo Y'). Busca la canción y la "
        "suma a la playlist; avisa si ya estaba.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "La canción recomendada, idealmente 'título artista' (ej. 'Flaca Calamaro')."}
            },
            "required": ["query"],
        },
        _tool_add_music,
        {Scope.REPLY, Scope.ADMIN},
    )
    reg.register(
        "share_video",
        "Trae un video de YouTube: con `query` busca sobre un tema; sin query trae uno de los "
        "más populares del momento (Argentina). Incluye el transcript si está disponible. Usala "
        "cuando un usuario pide un video/algo para ver, o cuando el agente quiere compartir un "
        "video en el feed. Devuelve título, canal, link y (si hay) transcript para comentar.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Opcional: tema/artista/palabras a buscar. Omitir para lo más popular del momento."}
            },
            "required": [],
        },
        _tool_share_video,
        {Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN},
    )
    reg.register(
        "get_news",
        "Trae titulares recientes con su link de las fuentes de noticias RSS configuradas "
        "(La Política Online, Página/12, etc.). Usala cuando un usuario pide noticias, "
        "titulares o links de actualidad. Se puede filtrar por categoría (ej. 'política').",
        {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Opcional: filtrar por categoría de la fuente (ej. 'política', 'noticias'). Omitir para todas."}
            },
            "required": [],
        },
        _tool_get_news,
        {Scope.REPLY, Scope.ADMIN},
    )
    reg.register(
        "use_skill",
        "Carga las instrucciones de una skill del bot por nombre. Usala cuando el tema "
        "de la conversación coincida con una skill del índice de tu system prompt.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre exacto de la skill (del índice)."}
            },
            "required": ["name"],
        },
        _tool_use_skill,
        {Scope.REPLY, Scope.FEED_REFLECTION},
    )
    _register_admin_config_tools(reg)  # T30: config por comandos de admin
    # Lazy de verdad: el SDK de MCP se importa solo si hay algún server PRENDIDO.
    # Con la sección MCP presente pero todo enabled:false (el default), ni se carga
    # — y el bot arranca aunque el entorno no tenga el paquete `mcp` instalado.
    if mcp_config and any(cfg.get("enabled", True) for cfg in mcp_config.values()):
        import mcp_tools  # lazy: solo si hay servers MCP habilitados
        n = mcp_tools.register_mcp_tools(reg, mcp_config)
        log.info("MCP: %d tool(s) externas registradas", n)
    if config:
        reg.apply_config(config)
    reg.set_groups(USER_GROUPS, admin_handle=ADMIN_HANDLES,
                   feed_resolver=_make_group_feed_resolver(bsky) if bsky is not None else None)
    return reg


def _register_admin_config_tools(reg: ToolRegistry) -> None:
    """T30: tools scope ADMIN para ajustar la config desde la plataforma.

    Doble efecto: persistir a settings.json (validado, atómico — T22) y aplicar
    en vivo sobre los objetos que el loop consulta. Orden: persistir PRIMERO
    (si la validación rechaza, el runtime no se toca).
    """

    def _get_bot_config(args: dict, ctx: ToolContext) -> ToolResult:
        lines = ["config actual:"]
        off = [n for n in reg.names() if not reg.get(n).enabled]
        lines.append("tools apagadas: " + (", ".join(off) or "ninguna"))
        risky = [n for n in reg.names()
                 if reg.get(n).enabled and Scope.REPLY in reg.get(n).scopes]
        lines.append("tools con scope reply (públicas): " + ", ".join(sorted(risky)))
        restringidas = [n for n in reg.names() if reg.get(n).groups]
        if USER_GROUPS or restringidas:
            lines.append("grupos de usuarios: " + ("; ".join(
                f"{g}=[{', '.join(members)}]" for g, members in USER_GROUPS.items())
                or "ninguno definido"))
            lines.append("tools restringidas por grupo: " + (", ".join(
                f"{n}→{sorted(reg.get(n).groups)}" for n in sorted(restringidas)) or "ninguna"))
        if _RUNTIME_TASKS:
            lines.append("tareas: " + ", ".join(
                f"{t.name}={'on' if t.enabled else 'off'}"
                + (f" cada {t.interval_hours}h" if t.interval_hours else "")
                for t in _RUNTIME_TASKS))
        lines.append("feeds: " + ", ".join(
            f"{f['name']}={'on' if f.get('enabled', True) else 'off'} "
            f"({f.get('posting_policy', 'balanced')}, {f.get('interval_hours', 6)}h)"
            for f in FEEDS_CONFIG))
        lines.append(f"noticias (NEWS_ENABLED): {'on' if NEWS_ENABLED else 'off'}")
        if MOODS_CONFIG.get("enabled"):
            try:
                mode = str(MOODS_CONFIG.get("mode", "manual")).lower()
                m = current_mood(ctx.conn)
                extra = ""
                if mode == "auto":
                    st = _mood_state_get(ctx.conn)
                    if st and st.get("reason"):
                        extra = f" — «{st['reason'][:100]}»"
                sus = f", susceptibilidad {_mood_susceptibility():.1f}" if mode == "auto" else ""
                lines.append(f"mood: {mode}, hoy {(m.name if m else 'sin definir')}{extra}{sus}")
            except Exception:
                lines.append("mood: (estado no disponible)")
        else:
            lines.append("mood: off (tono normal)")
        try:
            prefs = dbmod.list_preferences(ctx.conn)
            n_like = sum(1 for p in prefs if p["kind"] == "like")
            lines.append(f"preferencias: modo {PREFS_MODE} — {n_like} gustos, "
                         f"{len(prefs) - n_like} disgustos")
            lines.append(f"memoria general: {len(dbmod.list_bot_memory(ctx.conn))} entradas (en DB)")
        except Exception:
            pass
        if BUDGET_CONFIG.get("enabled"):
            lines.append(f"budget diario: ${float(BUDGET_CONFIG.get('daily_usd', 1.0)):.2f} "
                         f"(announce={'on' if BUDGET_CONFIG.get('announce', True) else 'off'})")
        else:
            lines.append("budget diario: off (sin límite de gasto)")
        hb_override = load_text(HEARTBEAT_OVERRIDE_PATH).strip()
        hb_default = load_text(HEARTBEAT_CHECKLIST_PATH).strip()
        if hb_override:
            lines.append(f"heartbeat: orden puntual del admin vigente «{hb_override[:120]}» "
                         "(pisa al default)")
        elif hb_default:
            lines.append(f"heartbeat: instrucciones por defecto «{hb_default[:120]}»")
        else:
            lines.append("heartbeat: sin instrucciones (solo calendario)")
        lines.append("mcp: " + (", ".join(
            f"{name}={'on' if cfg.get('enabled', True) else 'off'}"
            for name, cfg in MCP_CONFIG.items()) or "sin servers"))
        return ToolResult(text="\n".join(lines))

    def _set_tool_config(args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("tool") or "").strip()
        tool = reg.get(name)
        if tool is None:
            return ToolResult(text=f"tool desconocida: '{name}'. Válidas: {', '.join(reg.names())}")
        if name in _CONFIG_TOOL_NAMES:
            return ToolResult(text=f"'{name}' es una tool de configuración: no se toca "
                              "por comando (anti-lockout). Usá la UI (config_ui.py).")
        enabled, scopes = _as_bool(args.get("enabled")), args.get("scopes")
        groups = args.get("groups")
        if scopes is not None:
            ampliados = (set(scopes) - set(tool.scopes)) & _PUBLIC_SCOPES
            if ampliados:
                return ToolResult(text=f"no puedo AGREGAR scopes públicos ({sorted(ampliados)}) "
                                  "por comando — por acá solo se reduce exposición. "
                                  "Para ampliar, usá la UI (config_ui.py).")
        if groups is not None:
            actuales = set(tool.groups or ())
            nuevos = set(groups)
            if actuales and (not nuevos or nuevos - actuales):
                return ToolResult(text="no puedo AFLOJAR la restricción de grupos por comando "
                                  "(quitar la restricción o sumar grupos = más gente puede usar "
                                  "la tool). Para ampliar, usá la UI (config_ui.py).")
            desconocidos = nuevos - set(USER_GROUPS)
            if desconocidos:
                return ToolResult(text=f"grupo(s) desconocido(s): {sorted(desconocidos)}. "
                                  f"Definidos en USER_GROUPS: {sorted(USER_GROUPS) or 'ninguno'}.")

        def delta(s: dict) -> None:
            cfg = s.setdefault("TOOLS", {}).setdefault(name, {})
            if enabled is not None:
                cfg["enabled"] = enabled
            if scopes is not None:
                cfg["scopes"] = list(scopes)
            if groups is not None:
                cfg["groups"] = list(groups)
        errs = _persist_settings_delta(delta)
        if errs:
            return ToolResult(text="no apliqué nada: " + "; ".join(errs))
        if enabled is not None:
            tool.enabled = enabled
        if scopes is not None:
            tool.scopes = frozenset(set(scopes) & ALL_SCOPES)
        if groups is not None:
            tool.groups = frozenset(groups) if groups else None
        return ToolResult(text=f"tool {name}: "
                          + (f"enabled={enabled} " if enabled is not None else "")
                          + (f"scopes={sorted(tool.scopes)} " if scopes is not None else "")
                          + (f"groups={sorted(tool.groups or ())}" if groups is not None else "")
                          + " — aplicado en vivo y guardado")

    def _set_task_config(args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("task") or "").strip()
        known = ([t.name for t in _RUNTIME_TASKS] or list(TASKS_CONFIG)
                 or ["feed", "news", "mentions", "heartbeat"])
        if name not in known:
            return ToolResult(text=f"tarea desconocida: '{name}'. Válidas: {', '.join(known)}")
        enabled = _as_bool(args.get("enabled"))
        if name == "mentions" and enabled is False:
            return ToolResult(text="no puedo apagar 'mentions' por comando: es el canal "
                              "de estos mismos comandos (anti-lockout). Usá la UI.")
        interval = args.get("interval_hours")

        def delta(s: dict) -> None:
            cfg = s.setdefault("TASKS", {}).setdefault(name, {})
            if enabled is not None:
                cfg["enabled"] = enabled
            if interval is not None:
                cfg["interval_hours"] = float(interval)
        errs = _persist_settings_delta(delta)
        if errs:
            return ToolResult(text="no apliqué nada: " + "; ".join(errs))
        live = ""
        for t in _RUNTIME_TASKS:
            if t.name == name:
                if enabled is not None:
                    t.enabled = enabled
                if interval is not None:
                    t.interval_hours = float(interval)
                live = " — aplicado en vivo y"
        estado = ("la prendí" if enabled else "la apagué") if enabled is not None else "la ajusté"
        return ToolResult(text=f"listo, tarea {name}: {estado}"
                          + (f", corre {_fmt_interval(float(interval))}" if interval is not None else "")
                          + f"{live} guardado")

    def _set_heartbeat(args: dict, ctx: ToolContext) -> ToolResult:
        """Todo-en-uno del heartbeat: instrucciones + frecuencia + on/off en UNA
        llamada (el flujo admin ejecuta una sola tool; un pedido compuesto tipo
        'cada 5 minutos hacé X' no debe perder la mitad)."""
        instructions = args.get("instructions")
        interval = args.get("interval_hours")
        enabled = _as_bool(args.get("enabled"))
        if instructions is None and interval is None and enabled is None:
            return ToolResult(text="decime qué cambio del heartbeat: instrucciones, "
                              "frecuencia y/o prendido/apagado")
        if interval is not None or enabled is not None:
            def delta(s: dict) -> None:
                cfg = s.setdefault("TASKS", {}).setdefault("heartbeat", {})
                if enabled is not None:
                    cfg["enabled"] = enabled
                if interval is not None:
                    cfg["interval_hours"] = float(interval)
            errs = _persist_settings_delta(delta)
            if errs:
                return ToolResult(text="no apliqué nada: " + "; ".join(errs))
            for t in _RUNTIME_TASKS:
                if t.name == "heartbeat":
                    if enabled is not None:
                        t.enabled = enabled
                    if interval is not None:
                        t.interval_hours = float(interval)
        if instructions is not None:
            text = instructions.strip()
            HEARTBEAT_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_OVERRIDE_PATH.write_text(text + "\n" if text else "", encoding="utf-8")
        partes = ["dale, heartbeat actualizado:"]
        if enabled is not None:
            partes.append("prendido" if enabled else "apagado")
        if interval is not None:
            partes.append(_fmt_interval(float(interval)))
        if instructions is not None:
            corto = (instructions.strip()[:140] + "…") if len(instructions.strip()) > 140 else instructions.strip()
            partes.append(f"y la instrucción vigente es: «{corto}»" if corto
                          else "y borré tu orden puntual (vuelvo a mi heartbeat de siempre)")
        partes.append("— aplicado en vivo y guardado")
        return ToolResult(text=" ".join(partes))

    def _set_feed_config(args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("name") or "").strip()
        live_feed = next((f for f in FEEDS_CONFIG if f.get("name") == name), None)
        if live_feed is None:
            return ToolResult(text=f"feed desconocido: '{name}'. Válidos: "
                              + ", ".join(f.get("name", "?") for f in FEEDS_CONFIG))
        enabled = _as_bool(args.get("enabled"))
        interval, policy = args.get("interval_hours"), args.get("posting_policy")

        def delta(s: dict) -> None:
            for f in s.get("FEEDS", []):
                if f.get("name") == name:
                    if enabled is not None:
                        f["enabled"] = enabled
                    if interval is not None:
                        f["interval_hours"] = float(interval)
                    if policy is not None:
                        f["posting_policy"] = policy
        errs = _persist_settings_delta(delta)
        if errs:
            return ToolResult(text="no apliqué nada: " + "; ".join(errs))
        if enabled is not None:
            live_feed["enabled"] = enabled
        if interval is not None:
            live_feed["interval_hours"] = float(interval)
        if policy is not None:
            live_feed["posting_policy"] = policy
        return ToolResult(text=f"feed {name} actualizado — aplicado en vivo y guardado")

    def _set_news_enabled(args: dict, ctx: ToolContext) -> ToolResult:
        enabled = _as_bool(args.get("enabled"))
        if enabled is None:
            return ToolResult(text="falta el argumento enabled (true/false)")

        def delta(s: dict) -> None:
            s["NEWS_ENABLED"] = enabled
        errs = _persist_settings_delta(delta)
        if errs:
            return ToolResult(text="no apliqué nada: " + "; ".join(errs))
        global NEWS_ENABLED
        NEWS_ENABLED = enabled
        return ToolResult(text=f"noticias {'activadas' if enabled else 'desactivadas'} "
                          "— aplicado en vivo y guardado")

    def _set_mcp_enabled(args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("server") or "").strip()
        if name not in MCP_CONFIG:
            return ToolResult(text=f"server MCP desconocido: '{name}'. Válidos: "
                              + (", ".join(MCP_CONFIG) or "ninguno"))
        enabled = _as_bool(args.get("enabled"))
        if enabled is None:
            return ToolResult(text="falta el argumento enabled (true/false)")

        def delta(s: dict) -> None:
            s.setdefault("MCP", {}).setdefault(name, {})["enabled"] = enabled
        errs = _persist_settings_delta(delta)
        if errs:
            return ToolResult(text="no apliqué nada: " + "; ".join(errs))
        MCP_CONFIG.setdefault(name, {})["enabled"] = enabled
        return ToolResult(text=f"server MCP {name}: enabled={enabled} — guardado; "
                          "REINICIÁ el bot para que aplique (spawn de procesos)")

    _obj = {"type": "object"}
    reg.register("get_bot_config",
                 "Muestra la configuración actual del bot (tools, tareas, feeds, noticias, MCP).",
                 {**_obj, "properties": {}}, _get_bot_config, {Scope.ADMIN})
    reg.register("set_tool_config",
                 "Prende/apaga una tool del bot, cambia sus scopes o la restringe a grupos de "
                 "usuarios (USER_GROUPS). Cambio en vivo + persistido.",
                 {**_obj, "properties": {
                     "tool": {"type": "string", "description": "Nombre exacto de la tool."},
                     "enabled": {"type": "boolean"},
                     "scopes": {"type": "array", "items": {"type": "string",
                                "enum": ["reply", "feed_reflection", "admin"]}},
                     "groups": {"type": "array", "items": {"type": "string"},
                                "description": "Grupos de USER_GROUPS que pueden usar la tool. "
                                               "Por comando solo se puede RESTRINGIR (agregar una "
                                               "restricción donde no había, o achicar la lista)."},
                 }, "required": ["tool"]}, _set_tool_config, {Scope.ADMIN})
    reg.register("set_task_config",
                 "Prende/apaga una tarea periódica (feed, news, mentions) o cambia su intervalo en horas. "
                 "Para el heartbeat usá set_heartbeat.",
                 {**_obj, "properties": {
                     "task": {"type": "string"},
                     "enabled": {"type": "boolean"},
                     "interval_hours": {"type": "number"},
                 }, "required": ["task"]}, _set_task_config, {Scope.ADMIN})
    reg.register("set_heartbeat",
                 "Ajusta el heartbeat en una sola llamada: una orden puntual para los próximos pases "
                 "(instructions — es un override AUXILIAR que pisa temporalmente el heartbeat por "
                 "defecto, no lo reemplaza para siempre), cada cuánto corre (interval_hours; 5 minutos "
                 "= 0.0833) y si está prendido (enabled). Usala para CUALQUIER pedido sobre el "
                 "heartbeat; si el admin da una instrucción de qué hacer, casi siempre enabled=true.",
                 {**_obj, "properties": {
                     "instructions": {"type": "string",
                                      "description": "Orden puntual para los próximos pases, en lenguaje "
                                                     "natural. String vacío = borrar el override (vuelve "
                                                     "al heartbeat por defecto)."},
                     "interval_hours": {"type": "number"},
                     "enabled": {"type": "boolean"},
                 }}, _set_heartbeat, {Scope.ADMIN})
    reg.register("set_feed_config",
                 "Ajusta un feed proactivo: enabled, intervalo en horas y/o política de posteo.",
                 {**_obj, "properties": {
                     "name": {"type": "string"},
                     "enabled": {"type": "boolean"},
                     "interval_hours": {"type": "number"},
                     "posting_policy": {"type": "string",
                                        "enum": ["conservative", "balanced", "active"]},
                 }, "required": ["name"]}, _set_feed_config, {Scope.ADMIN})
    reg.register("set_news_enabled",
                 "Prende/apaga el posteo de noticias RSS (master switch NEWS_ENABLED).",
                 {**_obj, "properties": {"enabled": {"type": "boolean"}},
                  "required": ["enabled"]}, _set_news_enabled, {Scope.ADMIN})
    reg.register("set_mcp_enabled",
                 "Habilita/deshabilita un server MCP (reddit, browser). Requiere reiniciar el bot.",
                 {**_obj, "properties": {"server": {"type": "string"},
                                          "enabled": {"type": "boolean"}},
                  "required": ["server", "enabled"]}, _set_mcp_enabled, {Scope.ADMIN})


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

class ClassifyNode:
    """
    Node 1: Classify the mention.
    Uses LITE_MODEL — detecting a /command doesn't need reasoning.
    """

    def __init__(self, llm: RoleLLM):
        self.llm = llm

    def run(self, state: MentionState) -> dict:
        # T23: prompt en prompts/classify_prompt.md. Reconoce comandos con '/' Y
        # órdenes en lenguaje natural sobre config/comportamiento (T30) — el gate
        # de admin lo aplica route_after_classify, no este nodo.
        system = load_text(PROMPTS_DIR / "classify_prompt.md")
        try:
            result = self.llm.complete(system, f"Mention: {state['mention_text']}", MentionClassification)
        except Exception as e:
            log.error("ClassifyNode failed: %s", e)
            result = MentionClassification(is_admin_command=False, command=None, skip=False)

        log.info("Classified: is_command=%s command=%r skip=%s", result.is_admin_command, result.command, result.skip)
        return {"classification": result}


class LoadContextNode:
    """
    Node 2: Ensure the user exists in the DB. For first-time users, fetch the
    Bluesky bio, interpret it, and ingest the extracted facts into user_facts.
    No usa archivos .md — la recuperación por relevancia la hace GenerateReplyNode
    vía dbmod.hybrid_search_user_facts.
    """

    def __init__(self, llm: RoleLLM, bsky: BskyClient, conn: sqlite3.Connection):
        self.llm  = llm
        self.bsky = bsky
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        handle = state["author_handle"]
        self.conn.execute(
            "INSERT INTO users(handle) VALUES (?) ON CONFLICT(handle) DO NOTHING",
            (handle,),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT bio_raw, bio_interp FROM users WHERE handle = ?", (handle,)
        ).fetchone()
        if row["bio_raw"] is None and row["bio_interp"] is None:
            try:
                self._ingest_bio(handle)
            except Exception as e:
                # La bio es enriquecimiento: jamás debe bloquear el reply (y menos
                # el arranque, vía retry_stuck_mentions).
                log.error("ingest_bio @%s falló (%s: %s) — sigo sin bio",
                          handle, type(e).__name__, e)

        return {}  # el contexto relevante se recupera en GenerateReplyNode

    def _ingest_bio(self, handle: str) -> None:
        """Trae el profile de Bluesky, interpreta la bio y guarda todo + facts en la DB.
        También cachea `did` y `display_name` (el DID es estable ante cambios de handle
        y lo usa HandleBlockQueryNode sin re-fetchear)."""
        profile = self.bsky.get_profile(handle)
        if not profile:
            return
        # ¿El DID ya vive en otra fila? → el usuario se cambió el handle (el DID es
        # estable): migrar su memoria a este handle antes del UPDATE (users.did es
        # UNIQUE; sin esto, IntegrityError).
        old = self.conn.execute(
            "SELECT handle FROM users WHERE did = ? AND handle != ?",
            (profile.did, handle),
        ).fetchone()
        if old:
            moved = dbmod.migrate_user_handle(self.conn, old["handle"], handle)
            log.info("handle cambiado: @%s → @%s (mismo DID) — migrados %d facts, %d events",
                     old["handle"], handle, moved["facts"], moved["events"])
        bio = profile.description or ""
        interp = self._extract_from_bio(handle, bio) if bio else ""
        bio_interp = None if (not interp or interp.upper() == "NADA") else interp
        self.conn.execute(
            "UPDATE users SET did = ?, display_name = ?, bio_raw = ?, bio_interp = ?, "
            "updated_at = datetime('now') WHERE handle = ?",
            (profile.did, profile.display_name or None, bio or None, bio_interp, handle),
        )
        self.conn.commit()
        if bio_interp:
            for line in bio_interp.splitlines():
                fact = line.strip().lstrip("-").strip()
                if fact:
                    dbmod.upsert_user_fact(self.conn, handle, fact, source_uri="bio")

    def _extract_from_bio(self, handle: str, bio: str) -> str:
        if not bio:
            return ""
        prompt = load_text(PROMPTS_DIR / "interpret_bio_prompt.md")
        if not prompt:
            log.warning("interpret_bio_prompt.md not found")
            return ""
        try:
            content = self.llm.chat(messages=[
                {"role": "system", "content": prompt.format(handle=handle)},
                {"role": "user",   "content": f"Bio de @{handle}: {bio}"},
            ])
            return (content or "").strip()
        except Exception as e:
            log.error("Bio extraction failed for @%s: %s", handle, e)
            return ""


class GenerateReplyNode:
    """
    Node 3: Generate reply.
    Context = SOUL.md + MEMORY.md + user profile.
    """

    def __init__(self, llm: RoleLLM, conn: sqlite3.Connection, registry: ToolRegistry | None = None):
        self.llm      = llm
        self.conn     = conn
        self.registry = registry

    def _other_participants_facts(self, thread: str, *, author: str, query: str,
                                  max_users: int = 3, k: int = 3) -> str:
        """Facts de los demás participantes del hilo (excluye autor y bot).
        Barato: una hybrid_search local por participante, sin LLM. Best-effort."""
        if not thread:
            return ""
        try:
            others = [h for h in _thread_participants(thread)
                      if h not in (author, BSKY_HANDLE)][:max_users]
            sections = []
            for other in others:
                if not dbmod.user_exists(self.conn, other):
                    continue
                ofacts = dbmod.hybrid_search_user_facts(self.conn, other, query, k=k)
                if ofacts:
                    sections.append(f"@{other}:\n" + "\n".join(f"- {t}" for _, t in ofacts))
            if not sections:
                return ""
            return ("\n---\nHechos que sabés de otros participantes del hilo:\n"
                    + "\n".join(sections))
        except Exception:
            log.debug("other_participants_facts falló", exc_info=True)
            return ""

    def run(self, state: MentionState) -> dict:
        soul   = load_text(CONTEXT_DIR / "SOUL.md") or load_text(PROMPTS_DIR / "SOUL.md")
        handle = state["author_handle"]

        thread  = state.get("thread_context", "")
        current = f"{handle}: {state['mention_text']}"
        query   = f"{thread}\n{current}" if thread else current

        facts   = dbmod.hybrid_search_user_facts(self.conn, handle, query, k=5)
        lessons = dbmod.hybrid_search_lessons(self.conn, query, k=5)
        recent  = dbmod.recent_interactions(self.conn, handle, limit=5)

        parts = [soul, f"\n---\n{current_datetime_line()}"]
        mood = mood_line(self.conn)
        if mood:
            parts.append(mood)
        for block in (memory_block(self.conn), prefs_block(self.conn),
                      calendar_block(self.conn, handle=handle)):
            if block:
                parts.append(block)
        if facts:
            parts.append("\n---\nHechos que sabés del usuario:\n" + "\n".join(f"- {t}" for _, t in facts))
        if recent:
            parts.append(
                "\n---\nTus últimas conversaciones con este usuario (de más nueva a más vieja):\n"
                + "\n".join(f"- [{r['created_at'][:10]}] {r['summary']}" for r in recent))
        # Facts de los OTROS participantes del hilo (no solo el autor): el bot
        # ve la conversación completa, que sepa con quiénes está hablando.
        others_block = self._other_participants_facts(thread, author=handle, query=query)
        if others_block:
            parts.append(others_block)
        if lessons:
            parts.append("\n---\nLecciones de comportamiento:\n" + "\n".join(f"- {t}" for _, t in lessons))

        # T26: skills — inline las marcadas + índice on-demand (use_skill)
        skills_block = skills_prompt_block(SKILLS_DIR, Scope.REPLY)
        if skills_block:
            parts.append(f"\n---\n{skills_block}")

        # Resumen del catálogo de imágenes
        cat_stats = dbmod.get_image_catalog_stats(self.conn)
        if cat_stats["total"] > 0:
            cat_lines = [f"Total: {cat_stats['total']} imágenes disponibles"]
            for entry in cat_stats["by_category"]:
                cat_lines.append(
                    f"  [{entry['category']}] {entry['platform']}/{entry['source_name']}: {entry['count']}"
                )
            parts.append("\n---\nCatálogo de imágenes disponibles:\n" + "\n".join(cat_lines))
            parts.append(
                "Si el usuario te pide una imagen, meme o foto, seteá el campo "
                "'image_search_query' con palabras clave de búsqueda en español."
            )
        else:
            parts.append("\n---\nNo hay imágenes disponibles en el catálogo.")

        # Fase de tools scope REPLY (toggleable por config; hoy: summarize_feed).
        # Si el LLM decide llamar una tool, su resultado se inyecta al contexto.
        if self.registry is not None:
            # Filtrado por grupos de usuario (USER_GROUPS): las tools restringidas
            # ni se le ofrecen al LLM si el autor no pertenece.
            reply_tools = self.registry.openai_schemas(Scope.REPLY, handle=handle)
            if reply_tools:
                tool_names = [t["function"]["name"] for t in reply_tools]
                log.info("GenerateReplyNode: fase de tools con %d disponibles: %s",
                         len(tool_names), ", ".join(tool_names))
                # System liviano y enfocado en decidir tools (como el nodo admin);
                # NO el volcado de persona, que empuja al modelo a charlar en vez de
                # llamar una tool. El SOUL completo se usa en la fase 2 (el reply real).
                tool_system = f"{current_datetime_line()}\n\n" + load_text(PROMPTS_DIR / "reply_tools_prompt.md")
                try:
                    text_out, tool_calls = self.llm.call_with_tools(tool_system, query, reply_tools)
                    if not tool_calls:
                        log.info("GenerateReplyNode: el modelo NO llamó ninguna tool; "
                                 "respondió texto: %r", (text_out or "")[:200])
                    for call in tool_calls or []:
                        cname = call.function.name
                        cargs = json.loads(call.function.arguments)
                        log.info("GenerateReplyNode: llamando tool %s con args %s", cname, cargs)
                        outcome = self.registry.execute(cname, cargs, ToolContext(state=state, conn=self.conn),
                                                        handle=handle)
                        log.info("GenerateReplyNode: resultado de %s: %r", cname, (outcome.text or "")[:200])
                        parts.append(f"\n---\nResultado de {cname}: {outcome.text}")
                except Exception as e:
                    log.warning("GenerateReplyNode: fase de tools falló: %s", e)

        # T23: bloque de formato externalizado en prompts/reply_format.md
        parts.append("\n---\n" + load_text(PROMPTS_DIR / "reply_format.md"))
        system = "\n".join(parts)
        user   = query

        log_llm_context(f"reply → @{handle}", system, user)
        try:
            result = self.llm.complete(system, user, BotReply)
        except Exception as e:
            log.error("GenerateReplyNode failed: %s", e)
            return {"error": str(e)}

        return {
            "reply_text": result.text,
            "should_update_profile": result.should_update_profile,
            "image_path": self._resolve_image(result.image_search_query),
        }

    def _resolve_image(self, search_query: str | None) -> str | None:
        """Busca una imagen en el catálogo (con guardrail T12), o None."""
        return resolve_catalog_image(self.conn, search_query)


class HandleAdminCommandNode:
    """
    Node 3b: Handle admin commands using tool calling.
    The LLM receives the command text and a set of tools. Ejecuta TODAS las tool
    calls que pida (contenido: música, búsqueda, etc.) y concatena los resultados
    como respuesta; las tools de CONFIG mantienen "un cambio por mensaje" (solo
    la primera corre — guarda T30).
    """

    def __init__(self, llm: RoleLLM, conn: sqlite3.Connection, registry: ToolRegistry):
        self.llm      = llm
        self.conn     = conn
        self.registry = registry

    def run(self, state: MentionState) -> dict:
        log.info("Admin command de @%s: %r", state["author_handle"], state["mention_text"])
        # T23: prompt externalizado en prompts/admin_command_prompt.md
        system = f"{current_datetime_line()}\n\n" + load_text(PROMPTS_DIR / "admin_command_prompt.md")
        # T26: skills scope admin — todo inline (este flujo ejecuta UNA tool y
        # usa su resultado como respuesta: use_skill no encadenaría)
        skills_block = skills_prompt_block(SKILLS_DIR, Scope.ADMIN, all_inline=True)
        if skills_block:
            system += f"\n\n---\n{skills_block}"
        user = (
            f"Admin: @{state['author_handle']}\n"
            f"Comando recibido: {state['mention_text']}"
        )

        admin_tools = self.registry.openai_schemas(Scope.ADMIN)
        log_llm_context(f"admin → @{state['author_handle']}", system, user)
        text_reply, tool_calls = self.llm.call_with_tools(system, user, admin_tools)

        # No tool called — use direct text response as fallback
        if not tool_calls:
            log.warning("HandleAdminCommandNode: no tool called, using direct reply")
            return {"reply_text": text_reply or "comando no reconocido"}

        # Ejecuta TODAS las tool calls del modelo ("agregá 3 temas" = 3 llamadas),
        # con una excepción: las tools de CONFIG mantienen la regla "un cambio por
        # mensaje" (guarda de seguridad T30) — solo la primera de ellas corre, el
        # resto se saltea con aviso.
        texts: list[str] = []
        image_path: str | None = None
        config_done = False
        for call in tool_calls:
            tool_name = call.function.name
            tool_args = json.loads(call.function.arguments)
            if tool_name in _CONFIG_TOOL_NAMES and config_done:
                log.info("Tool de config extra salteada: %s (un cambio por mensaje)", tool_name)
                texts.append(f"[{tool_name}: salteada — un cambio de config por mensaje, "
                             "mandá el resto de a uno]")
                continue
            log.info("Tool called: %s(%s)", tool_name, tool_args)
            outcome = self.registry.execute(tool_name, tool_args, ToolContext(state=state, conn=self.conn))
            if tool_name in _CONFIG_TOOL_NAMES:
                config_done = True
            texts.append(outcome.text)
            image_path = image_path or outcome.image_path

        result: dict = {"reply_text": "\n".join(t for t in texts if t)}
        if image_path:
            result["image_path"] = image_path
        return result


class HandleRoleQueryNode:
    """
    Node 3d: /check-role — responde el rol y permisos del usuario, deterministamente
    (sin LLM). Se calcula desde el modelo REAL de permisos (ADMIN_HANDLE + USER_GROUPS
    del registry), no desde el SOUL. Abierto a cualquier usuario: route_after_classify
    lo deriva antes del gate admin. Reply ≤ 300 chars (límite de Bluesky).
    """

    def __init__(self, registry: ToolRegistry, conn: sqlite3.Connection):
        self.registry = registry
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        handle = state["author_handle"]
        base = "mencionarme y charlar, /remember, /bloques, /check-role"
        if state.get("is_admin") or is_admin_handle(handle):
            return {"reply_text": (
                f"sos el admin (@{handle}): acceso total — configuración, memoria, "
                f"tareas programadas y todos los comandos del bot.")[:300]}
        groups = self.registry.groups_for(handle)
        tools  = [t.name for t in self.registry.available(Scope.REPLY, handle)]
        extra  = f" y estas tools: {', '.join(tools)}" if tools else ""
        if groups:
            reply = f"sos parte de {', '.join(groups)}. podés: {base}{extra}."
        else:
            reply = f"sos de la comunidad (sin rol especial). podés: {base}{extra}."
        return {"reply_text": reply[:300]}


class HandleBlockQueryNode:
    """
    Node 3c: Responde "quién me bloquea" consultando ClearSky (proxy + cache).
    Sin LLM — la reply se arma deterministamente. Abierto a cualquier usuario:
    route_after_classify lo deriva acá antes del gate admin, así /bloques funciona
    en --mode open para todos y en admin_only para Polci (el filtro de process_mention
    ya descartó a los no-admin antes de llegar al grafo).

    Flujo: resolver DID del autor → cache TTL 1h o fetch ClearSky → resolver handles
    de los bloqueadores → formatear reply ≤ 300 chars. Fallback graceful si ClearSky
    cae o no se puede resolver el DID.
    """

    CLEARSKY_UI_URL = "https://clearsky.app/bsky/{handle}"

    def __init__(self, bsky: BskyClient, conn: sqlite3.Connection):
        self.bsky = bsky
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        handle = state["author_handle"]
        did = self._get_author_did(handle)
        if not did:
            return {"reply_text": "no pude resolver tu cuenta ahora, probá más tarde"}

        blockers = dbmod.get_cached_blocklist(self.conn, did)
        if blockers is None:
            try:
                blockers = clearsky_mod.get_blocked_by(did)
            except clearsky_mod.ClearSkyError as e:
                log.warning("ClearSky failed for %s: %s", did, e)
                return {"reply_text": "no pude consultar ClearSky ahora, probá en un rato"}
            dbmod.save_blocklist_cache(self.conn, did, blockers)

        if not blockers:
            return {"reply_text": "por ahora nadie te tiene bloqueado, según ClearSky. 🌤️"}

        dids = [b["did"] for b in blockers]
        handle_map = self.bsky.get_profile_handles(dids)
        return {"reply_text": self._format_reply(handle, blockers, handle_map)}

    def _get_author_did(self, handle: str) -> str | None:
        """DID del autor: cacheado en users.did, o fresco vía get_profile.
        Asegura la fila en users (el path de block-query saltea LoadContextNode)."""
        self.conn.execute(
            "INSERT INTO users(handle) VALUES (?) ON CONFLICT(handle) DO NOTHING",
            (handle,),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT did FROM users WHERE handle = ?", (handle,)
        ).fetchone()
        if row and row["did"]:
            return row["did"]
        did = self.bsky.resolve_did(handle)
        if did:
            self.conn.execute(
                "UPDATE users SET did = ?, updated_at = datetime('now') WHERE handle = ?",
                (did, handle),
            )
            self.conn.commit()
        return did

    def _format_reply(
        self,
        author_handle: str,
        blockers: list[dict],
        handle_map: dict[str, str],
    ) -> str:
        total = len(blockers)
        handles = [handle_map.get(b["did"]) for b in blockers]
        known = [h for h in handles if h]
        if total <= 8:
            if known:
                names = ", ".join(f"@{h}" for h in known)
                return f"Te bloquean {total}: {names}. (datos de ClearSky)"
            return f"Te bloquean {total} cuentas que no pude resolver por handle. (ClearSky)"
        sample = ", ".join(f"@{h}" for h in known[:5]) or "—"
        url = self.CLEARSKY_UI_URL.format(handle=author_handle)
        return f"Te bloquean {total} cuentas. Algunas: {sample}… Listado completo: {url}"


class PostReplyNode:
    """
    Node 4: Post reply to Bluesky and update SQLite status.
    Status flow: pending → replied (success) or failed (error).
    """

    def __init__(self, bsky: BskyClient, db: sqlite3.Connection):
        self.bsky = bsky
        self.db   = db

    def run(self, state: MentionState) -> dict:
        uri = state["mention_uri"]

        if not state.get("reply_text"):
            log.warning("No reply text for %s — marking failed", uri)
            update_status(self.db, uri, "failed")
            return {"posted_reply_uri": None}

        try:
            image_path = state.get("image_path")
            posted_uri = self.bsky.reply(
                text       = state["reply_text"],
                parent_uri = uri,
                parent_cid = state["mention_cid"],
                root_uri   = state["thread_root_uri"],
                root_cid   = state["thread_root_cid"],
                media_path = image_path,
            )
            log.info("Replied to @%s: %s", state["author_handle"], state["reply_text"][:60])
            update_status(self.db, uri, "replied")
            log_bot_post(
                self.db,
                uri             = posted_uri,
                in_reply_to     = uri,
                reply_to_handle = state["author_handle"],
                text            = state["reply_text"],
            )
        except Exception as e:
            log.error("Failed to post reply: %s", e)
            update_status(self.db, uri, "failed")
            posted_uri = None

        return {"posted_reply_uri": posted_uri}


class ProfileUpdate(BaseModel):
    """Salida estructurada del post-reply: hechos duraderos + nota de interacción."""
    facts: list[str] = Field(
        default_factory=list,
        description="Hechos duraderos que el usuario reveló sobre sí mismo. Vacío si no hubo.",
    )
    interaction_summary: str = Field(
        default="",
        description="UNA línea: de qué se habló y en qué tono. Siempre presente.",
    )


class UpdateProfileNode:
    """
    Node 5: Post-reply, memoria en dos niveles.
    - `facts`: hechos duraderos autorrevelados → db.user_facts (dedup semántico).
      El LLM decide — la mayoría de las interacciones no revelan ninguno.
    - `interaction_summary`: nota breve de CADA interacción (tema + tono) →
      db.interactions (log cronológico, sin embeddings). Esto garantiza que
      ninguna conversación pasa sin dejar huella en la memoria del usuario.
    """

    def __init__(self, llm: RoleLLM, conn: sqlite3.Connection):
        self.llm  = llm
        self.conn = conn

    def run(self, state: MentionState) -> dict:
        handle  = state["author_handle"]
        thread  = state.get("thread_context", "")
        current = f"{handle}: {state['mention_text']}"
        bot_reply = state.get("reply_text", "")
        conversation = f"{thread}\nbot: {bot_reply}\n{current}" if thread else f"bot: {bot_reply}\n{current}"

        prompt = load_text(PROMPTS_DIR / "update_user_prompt.md")
        if not prompt:
            return {}

        try:
            result = self.llm.complete(
                f"{current_datetime_line()}\n\n{prompt.format(author_handle=handle)}",
                conversation,
                ProfileUpdate,
            )
        except Exception as e:
            log.error("UpdateProfileNode failed for @%s: %s", handle, e)
            return {}

        new_facts = 0
        for fact in result.facts:
            fact = fact.strip().lstrip("-").strip()
            if fact and dbmod.upsert_user_fact(self.conn, handle, fact, source_uri="reply") is not None:
                new_facts += 1
        summary = (result.interaction_summary or "").strip()
        if summary:
            dbmod.log_interaction(self.conn, handle, summary,
                                  source_uri=state.get("mention_uri"))
        if new_facts or summary:
            log.info("@%s: %d hecho(s) nuevo(s), interacción %s", handle, new_facts,
                     "anotada" if summary else "sin nota")
        return {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_after_classify(state: MentionState) -> str:
    cls = state.get("classification")
    log.info(
        "Routing: is_command=%s command=%r is_admin=%s skip=%s block_query=%s role_query=%s",
        cls.is_admin_command if cls else None,
        cls.command if cls else None,
        state.get("is_admin"),
        cls.skip if cls else None,
        cls.is_block_query if cls else None,
        cls.is_role_query if cls else None,
    )
    # Block query y role query van antes del gate admin: son abiertos a cualquiera.
    if cls and cls.is_block_query:
        return "handle_block_query"
    if cls and cls.is_role_query:
        return "handle_role_query"
    if cls and cls.skip:
        return "skip"
    if cls and cls.is_admin_command and state.get("is_admin"):
        return "handle_admin_command"
    return "load_context"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_graph(router: ModelRouter, bsky: BskyClient, db: sqlite3.Connection):
    """
    Flow:
        START → classify
            → skip                                          → END
            → handle_admin_command → post_reply → END
            → load_context → generate_reply → post_reply
                                                → update_profile → END
    Cada nodo recibe un RoleLLM ligado a su rol; el router hace fallback por endpoint.
    """
    registry = build_tool_registry(TOOLS_CONFIG, bsky=bsky, router=router, mcp_config=MCP_CONFIG)
    log.info("Tool registry: %s", ", ".join(registry.names()) or "(vacío)")

    # Lectura de media del hilo (video/GIF/imagen) → descripción vision en el
    # contexto. Gateado por READ_THREAD_MEDIA (ON por default), best-effort.
    if READ_THREAD_MEDIA:
        bsky.set_media_describer(make_media_describer(RoleLLM(router, "image_describe")))

    classify       = ClassifyNode(RoleLLM(router, "classify"))
    load_context   = LoadContextNode(RoleLLM(router, "bio_interp"), bsky, db)
    generate_reply = GenerateReplyNode(RoleLLM(router, "reply"), db, registry)
    handle_admin   = HandleAdminCommandNode(RoleLLM(router, "admin"), db, registry)
    handle_blocks  = HandleBlockQueryNode(bsky, db)
    handle_role    = HandleRoleQueryNode(registry, db)
    post_reply     = PostReplyNode(bsky, db)
    update_profile = UpdateProfileNode(RoleLLM(router, "update_profile"), db)

    g = StateGraph(MentionState)
    g.add_node("classify_mention",      classify.run)
    g.add_node("load_context",          load_context.run)
    g.add_node("generate_reply",        generate_reply.run)
    g.add_node("handle_admin_command",  handle_admin.run)
    g.add_node("handle_block_query",    handle_blocks.run)
    g.add_node("handle_role_query",     handle_role.run)
    g.add_node("post_reply",            post_reply.run)
    g.add_node("update_profile",        update_profile.run)

    g.add_edge(START, "classify_mention")
    g.add_conditional_edges(
        "classify_mention",
        route_after_classify,
        {
            "handle_admin_command" : "handle_admin_command",
            "handle_block_query"   : "handle_block_query",
            "handle_role_query"    : "handle_role_query",
            "load_context"         : "load_context",
            "skip"                 : END,
        },
    )
    g.add_edge("load_context",         "generate_reply")
    g.add_edge("generate_reply",       "post_reply")
    g.add_edge("handle_admin_command", "post_reply")
    g.add_edge("handle_block_query",   "post_reply")
    g.add_edge("handle_role_query",    "post_reply")
    g.add_edge("post_reply",           "update_profile")
    g.add_edge("update_profile",       END)

    return g.compile()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def process_mention(graph, db: sqlite3.Connection, mention: dict, mode: str) -> None:
    uri    = mention["uri"]
    author = mention["author_handle"]

    if has_replied(db, uri):
        log.debug("Already handled %s — skipping", uri)
        return

    if mode == "admin_only" and not is_admin_handle(author):
        log.info("admin_only: ignorando a @%s (no es admin)", author)
        mark_pending(db, uri, mention["cid"], author, mode)
        update_status(db, uri, "ignored")
        return

    initial_state: MentionState = {
        "mention_uri"          : uri,
        "mention_cid"          : mention["cid"],
        "mention_text"         : mention["text"],
        "author_handle"        : author,
        "thread_context"       : mention.get("thread_context", ""),
        "thread_root_uri"      : mention.get("thread_root_uri", uri),
        "thread_root_cid"      : mention.get("thread_root_cid", mention["cid"]),
        "mode"                 : mode,
        "is_admin"             : is_admin_handle(author),
        "classification"       : None,
        "reply_text"           : None,
        "should_update_profile": False,
        "image_path"           : None,
        "posted_reply_uri"     : None,
        "error"                : None,
    }

    mark_pending(db, uri, mention["cid"], author, mode)
    log.info("Processing mention from @%s", author)
    graph.invoke(initial_state)


def retry_stuck_mentions(graph, db: sqlite3.Connection, bsky: BskyClient, mode: str) -> None:
    """Reprocesa al arranque mentions que quedaron trancados.

    - 'pending' stale (el bot murió antes de pasarlos a 'replied'/'failed') → se
      pasan a 'failed' porque has_replied saltea 'pending' y nunca se reintentarían.
    - 'failed' que cayeron fuera de la ventana de las últimas 25 notifs → la poll
      de Bluesky no los vuelve a traer, acá los refetcheamos por URI.

    Posts borrados (get_mention_by_uri → None) se marcan 'ignored' para no reintentarlos.
    """
    db.execute("UPDATE replied_posts SET status = 'failed' WHERE status = 'pending'")
    db.commit()
    rows = db.execute(
        "SELECT uri, cid, author, mode FROM replied_posts WHERE status = 'failed'"
    ).fetchall()
    if not rows:
        return
    log.info("Retrying %d stuck/failed mention(s) from last run", len(rows))
    for r in rows:
        mention = bsky.get_mention_by_uri(r["uri"])
        if mention is None:
            log.warning("Could not refetch %s (deleted?) — marking ignored", r["uri"])
            update_status(db, r["uri"], "ignored")
            continue
        try:
            process_mention(graph, db, mention, mode)
        except Exception as e:
            # Un retry que explota no puede matar el arranque: queda 'failed'
            # (o 'pending'→'failed' en el próximo arranque) y se reintenta después.
            log.error("retry de %s falló (%s: %s) — sigo con el resto",
                      r["uri"], type(e).__name__, e, exc_info=True)


def run(mode: str) -> None:
    log.info("Starting botata — mode: %s", mode.upper())

    db     = init_db()
    bsky   = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
    router = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
    log.info("Router de modelos: %s", router.describe())

    graph      = build_graph(router, bsky, db)
    registry   = build_tool_registry(TOOLS_CONFIG, bsky=bsky, router=router, mcp_config=MCP_CONFIG)
    feed_graph = build_feed_graph(router, bsky, db, registry)

    log.info("Graph compiled. Polling every %ds. Admin: %s", POLL_INTERVAL, ADMIN_HANDLE)
    log.info("Feeds configured: %d (loop proactivo T5/T6 integrado)", len(FEEDS_CONFIG))

    # Reprocesar mentions trancados del run anterior antes de entrar al loop.
    retry_stuck_mentions(graph, db, bsky, mode)

    # --- T27: registro de tareas periódicas. interval_hours=0 = cada iteración
    # (la tarea se auto-gatea por dentro); >0 = gate del scheduler vía cursor
    # `task:{name}`. Agregar una tarea nueva = una entrada acá + TASKS en settings.
    tasks = [
        PeriodicTask("feed",      lambda: _run_feed_pass(feed_graph, respect_interval=True)),
        PeriodicTask("news",      lambda: run_news_pass(bsky, router, db, respect_interval=True)),
        PeriodicTask("mentions",  lambda: _poll_mentions(graph, db, bsky, mode)),
        PeriodicTask("heartbeat", lambda: run_heartbeat_pass(bsky, router, db, registry),
                     interval_hours=12, enabled=False),  # outward-facing: el admin lo prende
        PeriodicTask("reflection", lambda: run_reflection_pass(router, db),
                     interval_hours=24, enabled=True),  # inward-only (no postea): destila lecciones
        PeriodicTask("public_reflection", lambda: run_public_reflection_pass(bsky, router, db),
                     interval_hours=24, enabled=True),  # outward: reflexión en primera persona
        PeriodicTask("playlist_share", lambda: run_playlist_share_pass(bsky, router, db),
                     interval_hours=24, enabled=True),  # outward: tema de la playlist comunitaria
        PeriodicTask("mood", lambda: run_mood_pass(router, db),
                     interval_hours=6, enabled=True),  # inward: decide el humor del día (solo modo auto)
    ]
    apply_tasks_config(tasks, TASKS_CONFIG)
    _RUNTIME_TASKS[:] = tasks  # T30: las tools de config del admin mutan esta lista en vivo
    log.info("Tareas: %s", ", ".join(
        f"{t.name}{'(off)' if not t.enabled else ''}" for t in tasks))

    guard = build_budget_guard(db)
    if guard.enabled:
        log.info("Budget diario: $%.2f (announce=%s)", guard.daily_usd,
                 BUDGET_CONFIG.get("announce", True))

    while True:
        try:
            # Guard económico: quemado el budget del día, el bot duerme (no corre
            # NINGUNA tarea, menciones incluidas) hasta el día siguiente.
            transition = guard.check()
            if transition:
                _announce_budget_transition(bsky, db, transition)
            if guard.burned:
                time.sleep(POLL_INTERVAL)
                continue
            run_due(
                tasks,
                get_last=lambda name: get_feed_last_run(db, name),
                save_last=lambda name: save_feed_last_run(db, name),
                network_errors=(BskyNetworkError, BskyRequestException),
            )
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        time.sleep(POLL_INTERVAL)


def build_budget_guard(conn: sqlite3.Connection) -> "budgetmod.BudgetGuard":
    """Arma el BudgetGuard desde la sección BUDGET de settings (UI-editable).
    El fetcher apunta al endpoint /credits de OpenRouter (el budget mide el gasto
    ahí; el fallback local Ollama es gratis y queda afuera a propósito)."""
    return budgetmod.BudgetGuard(
        get=lambda k: dbmod.kv_get(conn, k),
        set=lambda k, v: dbmod.kv_set(conn, k, v),
        fetch=lambda: budgetmod.fetch_openrouter_usage(OPENAI_ENDPOINT, OPENROUTER_API_KEY),
        daily_usd=float(BUDGET_CONFIG.get("daily_usd", 1.0)),
        enabled=bool(BUDGET_CONFIG.get("enabled", False)),
    )


def _announce_budget_transition(bsky: BskyClient, conn: sqlite3.Connection,
                                transition: str) -> None:
    """Postea el anuncio de siesta ('sleep') o despertar ('wake') si announce=true.
    Mensajes al azar de prompts/burned.json / hello.json (portados de maripobot).
    Best-effort: un fallo posteando jamás tira el loop."""
    if not BUDGET_CONFIG.get("announce", True):
        return
    path = PROMPTS_DIR / ("burned.json" if transition == "sleep" else "hello.json")
    fallback = ("Límite alcanzado por hoy, chau" if transition == "sleep"
                else "Bip bip, estoy activo de vuelta")
    try:
        msgs = load_json(path) or []
        text = random.choice(msgs) if msgs else fallback
        uri = bsky.post(text)
        log_bot_post(conn, uri=uri, in_reply_to=None, reply_to_handle=None, text=text)
        log.info("budget: anuncio de %s posteado", transition)
    except Exception as e:
        log.error("budget: no pude postear el anuncio de %s: %s", transition, e)


def _thread_participants(thread_context: str) -> list[str]:
    """Handles que participaron del thread. Las líneas del contexto las armamos
    nosotros con formato fijo `handle: texto` (_thread_context) → parseo seguro."""
    seen: list[str] = []
    for line in thread_context.splitlines():
        h = line.split(":", 1)[0].strip()
        if h and "." in h and " " not in h and h not in seen:  # forma de handle (dominio)
            seen.append(h)
    return seen


def _bump_thread_relationships(conn: sqlite3.Connection, author: str,
                               thread_context: str) -> None:
    """Grafo de relaciones: el autor de la mención interactuó con los participantes
    del thread. Mecánico y best-effort (solo entre usuarios ya conocidos — el gate
    vive en db.bump_relationship); jamás bloquea el procesamiento."""
    try:
        for other in _thread_participants(thread_context):
            if other not in (author, BSKY_HANDLE):
                dbmod.bump_relationship(conn, author, other, kind="thread")
    except Exception:
        log.error("bump_thread_relationships falló para @%s", author, exc_info=True)


def _poll_mentions(graph, db: sqlite3.Connection, bsky: BskyClient, mode: str) -> None:
    """Poll de menciones (extraído del loop en T27; comportamiento idéntico)."""
    mentions = [m for m in bsky.get_mentions() if not has_replied(db, m["uri"])]
    if mentions:
        log.info("Found %d mention(s) to process", len(mentions))
        for mention in mentions:
            ctx, root_uri, root_cid, leaf_media = bsky.get_thread_info(mention["uri"], mention["cid"])
            mention["thread_context"]  = ctx
            mention["thread_root_uri"] = root_uri
            mention["thread_root_cid"] = root_cid
            if leaf_media:  # media del post que menciona al bot (video/GIF/imagen)
                mention["text"] = f"{mention['text']} {leaf_media}".strip()
            _bump_thread_relationships(db, mention["author_handle"], ctx)
            process_mention(graph, db, mention, mode)
        # mark_all_read is UI hygiene only (keeps Polci's notif tab clean);
        # it no longer gates dedup — the DB does.
        bsky.mark_all_read()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="botata")
    parser.add_argument(
        "--mode",
        choices=["admin_only", "open"],
        default="admin_only",
    )
    parser.add_argument(
        "--fetch-feeds",
        action="store_true",
        help="Process all configured feeds once and exit (ignores interval check).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Use with --fetch-feeds: ignore last_run and fetch everything up to the pagination cap.",
    )
    parser.add_argument(
        "--post-summary",
        metavar="FEED_NAME",
        help="Generate and post an opinion about the latest summary of a feed, then exit.",
    )
    parser.add_argument(
        "--proactive",
        action="store_true",
        help="T5: run the autonomous feed loop (fetch→summarize→agent decides→maybe post) and exit.",
    )
    parser.add_argument(
        "--force-post",
        action="store_true",
        help="Use with --proactive: force a post per feed even if the agent would abstain.",
    )
    parser.add_argument(
        "--news",
        action="store_true",
        help="T15: run the RSS news pass once (post what's new per source) and exit. "
             "Requires NEWS_ENABLED=true in settings.json.",
    )
    parser.add_argument(
        "--heartbeat",
        action="store_true",
        help="T27: run the calendar heartbeat pass once (agent decides whether to post) "
             "and exit. Ignores the scheduler interval and the TASKS toggle.",
    )
    parser.add_argument(
        "--reflect",
        action="store_true",
        help="T6: run the reflection pass once (distill behavioral lessons from recent "
             "bot activity into db.lessons) and exit. Inward-only, never posts.",
    )
    parser.add_argument(
        "--reflect-public",
        action="store_true",
        help="Run the PUBLIC reflection pass once (first-person post about what the "
             "bot lived/learned, <=300 chars) and exit. Outward-facing.",
    )
    parser.add_argument(
        "--share-playlist",
        action="store_true",
        help="Post a random track from the community playlist once (LLM comment per "
             "TASKS.playlist_share.comment) and exit. Requires spotify_auth.py token.",
    )
    parser.add_argument(
        "--mood",
        action="store_true",
        help="Decide the bot's mood for today once (auto mode) and exit. Forces a "
             "recompute; no-op unless MOODS.enabled and MOODS.mode='auto'.",
    )
    args = parser.parse_args()

    if args.proactive:
        run_feed_loop(force_post=args.force_post, full_backfill=args.backfill)

    elif args.news:
        run_news_loop()

    elif args.heartbeat:
        run_heartbeat_loop()

    elif args.reflect:
        run_reflection_loop()

    elif args.reflect_public:
        run_public_reflection_loop()

    elif args.share_playlist:
        run_playlist_share_loop()

    elif args.mood:
        run_mood_loop()

    elif args.fetch_feeds:
        db        = init_db()
        bsky      = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
        router    = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
        processor = FeedProcessor(bsky, router, db)
        for feed in FEEDS_CONFIG:
            processor.process(
                feed_name      = feed["name"],
                list_uri       = feed["uri"],
                interval_hours = 0,
                full_backfill  = args.backfill,
            )
        log.info("Feed fetch complete.")

    elif args.post_summary:
        db        = init_db()
        bsky      = BskyClient(handle=BSKY_HANDLE, password=BSKY_PASSWORD)
        router    = build_router(MODELS_CONFIG, legacy=_LEGACY_MODELS, env=os.environ)
        processor = FeedProcessor(bsky, router, db)
        processor.post_opinion(feed_name=args.post_summary)

    else:
        run(mode=args.mode)