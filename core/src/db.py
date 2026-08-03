"""db.py — capa de persistencia de botata.

SQLite (WAL) como source of truth único + sqlite-vec (vec0, ANN coseno)
+ FTS5 (BM25) para búsqueda híbrida. El esquema y las decisiones de diseño
están documentados en CLAUDE.md, sección "Esquema de persistencia (DECIDIDO)".

Este módulo cubre solo la fundación: conexión, pragmas, carga de la
extensión sqlite-vec, esquema DDL completo y el cargador del modelo de
embeddings (bge-m3, 1024 dim). Las funciones de upsert/dedup y búsqueda
híbrida (vec + FTS5 + RRF) se agregan en el paso siguiente.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import sqlite_vec

log = logging.getLogger("botata.db")

# ─── Constantes ────────────────────────────────────────────────────────────
# T28c: la DB vive en el directorio de INSTANCIA (--instance / BOTATA_INSTANCE;
# default = raíz del repo, back-compat).
from instance import instance_dir  # noqa: E402

BASE_DIR = instance_dir()
DB_PATH = BASE_DIR / "posted" / "botata.db"

# bge-m3 (dense) → 1024 dimensiones. Verificado contra config.json del modelo.
EMBED_DIM = 1024
EMBED_MODEL_NAME = "BAAI/bge-m3"

# Cosine: vec0 devuelve distance = 1 - similitud. Umbral de dedup propuesto
# (cosine > 0.92 ⟺ distance < 0.08). Se usa en el paso de upsert/dedup.
DEDUP_THRESHOLD = 0.92

# ─── Zona horaria local de la instancia ─────────────────────────────────────
# Las fechas "humanas" (events.event_at, defaults de "ahora"/"hoy") se
# interpretan en la zona LOCAL de la instancia. Default: Argentina (UTC-3, sin
# DST); botata.py y config_ui.py la pisan al arrancar con set_local_tz(TIMEZONE).
LOCAL_TZ: Any = timezone(timedelta(hours=-3))


def set_local_tz(name: str) -> str:
    """Configura la zona local desde settings (TIMEZONE). Acepta nombre IANA
    ('America/Argentina/Buenos_Aires') u offset fijo ('UTC-3', 'UTC+5:30').
    Devuelve una descripción legible. Nombre inválido → warning y queda la
    zona vigente (un typo en settings no deja al bot sin reloj)."""
    global LOCAL_TZ
    name = (name or "").strip()
    if not name:
        return str(LOCAL_TZ)
    m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", name, re.I)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        LOCAL_TZ = timezone(sign * timedelta(hours=int(m.group(2)),
                                             minutes=int(m.group(3) or 0)))
        return name.upper()
    try:
        from zoneinfo import ZoneInfo
        LOCAL_TZ = ZoneInfo(name)
        return name
    except Exception:
        log.warning("TIMEZONE %r inválida (¿falta tzdata?) — sigo en %s", name, LOCAL_TZ)
        return str(LOCAL_TZ)


def local_now() -> datetime:
    """Ahora en la zona local de la instancia, naive (mismo formato que event_at)."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)

# ─── Esquema ────────────────────────────────────────────────────────────────
_SCHEMA = """
-- ─── users: identidad y perfil base ──────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    handle       TEXT PRIMARY KEY,          -- ej. 'ppolci.com' (sin @)
    did          TEXT UNIQUE,               -- Bluesky DID (estable ante cambio de handle)
    display_name TEXT,
    bio_raw      TEXT,                      -- bio literal de Bluesky
    bio_interp   TEXT,                      -- bullets extraídos por interpret_bio_prompt
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── user_facts: hechos autorrevelados (semántico) ───────────────────
CREATE TABLE IF NOT EXISTS user_facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    handle        TEXT NOT NULL REFERENCES users(handle) ON DELETE CASCADE,
    fact_text     TEXT NOT NULL,            -- ej. "Vive en Rosario"
    source_uri    TEXT,                     -- URI del post de origen (auditoría)
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    pinned        INTEGER NOT NULL DEFAULT 0,  -- 📌 lo pedido explícitamente: entra SIEMPRE
    superseded_by INTEGER REFERENCES user_facts(id) ON DELETE SET NULL  -- dedup/merge soft
);
CREATE INDEX IF NOT EXISTS idx_user_facts_handle  ON user_facts(handle);
CREATE INDEX IF NOT EXISTS idx_user_facts_created ON user_facts(created_at);

-- ─── lessons: lecciones conductuales (semántico, cross-user) ─────────
CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_text   TEXT NOT NULL,            -- ej. "Cuando X hace Y, responder Z funciona mejor"
    scope         TEXT NOT NULL DEFAULT 'community',  -- 'community' | 'user:ppolci.com'
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by INTEGER REFERENCES lessons(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_scope ON lessons(scope);

-- ─── relationships: grafo ponderado entre usuarios ───────────────────
-- aristas no dirigidas: la asimetría handle_a/handle_b es solo de storage;
-- consultar siempre por (handle_a = :h OR handle_b = :h).
CREATE TABLE IF NOT EXISTS relationships (
    handle_a TEXT NOT NULL REFERENCES users(handle) ON DELETE CASCADE,
    handle_b TEXT NOT NULL REFERENCES users(handle) ON DELETE CASCADE,
    kind     TEXT NOT NULL,                 -- 'reply'|'mention'|'thread'|'mutual'
    weight   REAL NOT NULL DEFAULT 1.0,     -- afinidad; decae con el tiempo (job de mantenimiento)
    last_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (handle_a, handle_b, kind)
);
CREATE INDEX IF NOT EXISTS idx_rel_a ON relationships(handle_a);
CREATE INDEX IF NOT EXISTS idx_rel_b ON relationships(handle_b);

-- ─── bot_posts: lo que el bot publicó (log de salida) ───────────────
CREATE TABLE IF NOT EXISTS bot_posts (
    uri             TEXT PRIMARY KEY,
    in_reply_to     TEXT,                   -- URI del post respondido (NULL si raíz)
    reply_to_handle TEXT REFERENCES users(handle),
    posted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    text            TEXT
);
CREATE INDEX IF NOT EXISTS idx_bot_posts_reply  ON bot_posts(in_reply_to);
CREATE INDEX IF NOT EXISTS idx_bot_posts_handle ON bot_posts(reply_to_handle);

-- ─── events: calendario / eventos (T4) ──────────────────────────────
-- handle NULL = evento de comunidad (sin dueño). event_at en ISO 8601
-- (fecha o fecha+hora). Fuente de las tools de calendar (T9) y del loop
-- proactivo (T6: aprendizajes del feed → eventos).
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handle      TEXT REFERENCES users(handle) ON DELETE CASCADE,  -- NULL = comunidad
    title       TEXT NOT NULL,
    description TEXT,
    event_at    TEXT NOT NULL,                    -- ISO 8601 (America/Argentina)
    kind        TEXT NOT NULL DEFAULT 'other',    -- birthday|reminder|community|other|bot_action
    source      TEXT,                             -- admin|feed|/comando|uri de origen
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    done        INTEGER NOT NULL DEFAULT 0        -- solo kind='bot_action': ya ejecutada
);
CREATE INDEX IF NOT EXISTS idx_events_at     ON events(event_at);
CREATE INDEX IF NOT EXISTS idx_events_handle ON events(handle);

-- ─── interactions: log conversacional por usuario ────────────────────
-- Una nota breve por CADA interacción directa (mención respondida): de qué se
-- habló, en qué tono. Separada de user_facts a propósito: los facts son datos
-- duraderos autorrevelados; esto es historial de conversación (recencia > semántica,
-- se recupera cronológico, sin embeddings).
CREATE TABLE IF NOT EXISTS interactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    handle     TEXT NOT NULL REFERENCES users(handle) ON DELETE CASCADE,
    summary    TEXT NOT NULL,               -- ej. "discutimos del mundial, tono jodón"
    source_uri TEXT,                        -- URI de la mención de origen
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Compactación (T48): una charla de ida y vuelta deja UNA fila por mención
    -- respondida, así que un rato de conversación genera varias notas casi
    -- iguales y tapa la ventana de recencia. Al compactar por día, las
    -- originales quedan archivadas acá en vez de borrarse.
    superseded_by INTEGER REFERENCES interactions(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_interactions_handle ON interactions(handle, created_at);

-- ─── bot_memory: memoria general del bot (reemplaza context/MEMORY.md) ──
-- Hechos de comunidad, directivas del admin y contexto de mundo que el bot
-- carga SIEMPRE (completa, sin retrieval — es chica y transversal). Antes
-- vivía en un archivo de texto (context/MEMORY.md), retirado: la DB es el
-- source of truth único.
CREATE TABLE IF NOT EXISTS bot_memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    source     TEXT,                            -- 'admin'|'tool:@handle'|'migration:MEMORY.md'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 📌 el pase de compactación no la toca (identidad del bot, o el admin la fijó)
    pinned     INTEGER NOT NULL DEFAULT 0,
    -- Compactación (T48): la fila sigue existiendo para auditoría/undo pero deja
    -- de entrar al contexto. NULL = vigente. Apunta a la fila que la reemplazó,
    -- o a sí misma cuando se descartó sin sucesora (efímero, basura).
    superseded_by INTEGER REFERENCES bot_memory(id) ON DELETE SET NULL
);

-- ─── preferences: gustos y disgustos del bot ─────────────────────────
-- Capa de identidad EDITABLE (a diferencia de SOUL.md): el admin siempre
-- puede tocarla (tools admin + UI); el bot según PREFS.mode (manual |
-- add_only | full_auto). Se inyecta completa en los prompts outward.
CREATE TABLE IF NOT EXISTS preferences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('like','dislike')),
    text       TEXT NOT NULL,
    source     TEXT,                            -- 'admin' | 'bot'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── replied_posts: idempotencia de procesamiento (entrada) ─────────
-- (preexistente en botata.py — se preserva tal cual)
CREATE TABLE IF NOT EXISTS replied_posts (
    uri        TEXT PRIMARY KEY,
    cid        TEXT NOT NULL,
    author     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|replied|failed|ignored
    replied_at TEXT NOT NULL,
    mode       TEXT NOT NULL,
    -- intentos fallidos acumulados (T55): 'failed' se reintenta en cada poll,
    -- así que sin contador una caída de endpoints es un loop infinito.
    attempts   INTEGER NOT NULL DEFAULT 0
);

-- ─── feed_cursors: cursores por feed ─────────────────────────────────
-- (preexistente en botata.py — se preserva tal cual)
CREATE TABLE IF NOT EXISTS feed_cursors (
    feed_name  TEXT PRIMARY KEY,
    last_run   TEXT
);

-- ─── kv: estado misceláneo clave→valor ───────────────────────────────
-- Para estado chico que debe sobrevivir reinicios sin merecer tabla propia
-- (ej. 'budget_state' del guard de presupuesto). Valores JSON como TEXT.
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── posted_news: idempotencia de items RSS ya posteados (T15) ────────
CREATE TABLE IF NOT EXISTS posted_news (
    item_id   TEXT PRIMARY KEY,   -- link o guid del item RSS
    source    TEXT,               -- host de la fuente
    title     TEXT,
    posted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── clearsky_cache: cache de bloqueadores por DID ────────────────────
-- Proxy de la API pública de ClearSky ("quién me bloquea"). TTL corto: los
-- bloques no cambian seguido y amortiguamos repetición del comando. El JSON
-- guarda la lista cruda de {did, blocked_date} devuelta por ClearSky.
CREATE TABLE IF NOT EXISTS clearsky_cache (
    did           TEXT PRIMARY KEY,
    blockers_json TEXT NOT NULL,        -- JSON array de {did, blocked_date}
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── image_catalog: mapa de conocimiento de imágenes del bot ─────────
-- Cada archivo de imagen en scrape/pictures/ tiene una fila acá con su
-- descripción generada por LLM multimodal, categoría, tags y OCR. Es la
-- fuente de verdad para que el bot sepa qué imágenes tiene disponibles.
CREATE TABLE IF NOT EXISTS image_catalog (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,              -- 'instagram' | 'reddit' | 'local'
    external_id   TEXT NOT NULL,              -- post shortcode (= sidecar de Membrilla)
    source_name   TEXT NOT NULL,              -- cuenta de origen (ej. 'encadenado_shitpost')
    file_path     TEXT NOT NULL,              -- relativo a repo root
    description   TEXT NOT NULL,              -- descripción LLM de qué hay en la imagen
    category      TEXT NOT NULL,              -- 'meme' | 'foto' | 'arte' | 'captura' | 'otro'
    tags          TEXT NOT NULL DEFAULT '[]', -- JSON array: ["shitpost", "argentina", "gato"]
    ocr_text      TEXT,                       -- texto visible en la imagen (si tiene)
    source_url    TEXT,                       -- permalink al post original
    posted_at     TEXT,                       -- fecha del post original
    used_at       TEXT,                       -- última vez que botata usó esta imagen
    use_count     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, external_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_image_cat_platform ON image_catalog(platform, source_name);
CREATE INDEX IF NOT EXISTS idx_image_cat_category ON image_catalog(category);
CREATE INDEX IF NOT EXISTS idx_image_cat_used    ON image_catalog(used_at);

-- ─── Búsqueda semántica: sqlite-vec (vec0, cosine) ───────────────────
-- distance_metric=cosine → distance = 1 - cos_sim. rowid == id de la tabla base.
-- user_facts_vec particionada por handle: el dedup y la recuperación de hechos
-- del autor se restringen a esa partición (sin contaminar con hechos ajenos).
CREATE VIRTUAL TABLE IF NOT EXISTS user_facts_vec USING vec0(
    embedding FLOAT[1024] distance_metric=cosine,
    partition_key TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_vec USING vec0(
    embedding FLOAT[1024] distance_metric=cosine
);
CREATE VIRTUAL TABLE IF NOT EXISTS image_catalog_vec USING vec0(
    embedding FLOAT[1024] distance_metric=cosine
);

-- ─── Búsqueda keyword: FTS5 (BM25) ───────────────────────────────────
-- content='external': el texto vive en la tabla base; FTS5 solo indexa.
CREATE VIRTUAL TABLE IF NOT EXISTS user_facts_fts USING fts5(
    fact_text,
    content='user_facts', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(
    lesson_text,
    content='lessons', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS image_catalog_fts USING fts5(
    description, tags, ocr_text,
    content='image_catalog', content_rowid='id'
);

-- triggers: mantener FTS5 sincronizado con la tabla base (user_facts)
CREATE TRIGGER IF NOT EXISTS user_facts_ai AFTER INSERT ON user_facts BEGIN
  INSERT INTO user_facts_fts(rowid, fact_text) VALUES (new.id, new.fact_text);
END;
CREATE TRIGGER IF NOT EXISTS user_facts_ad AFTER DELETE ON user_facts BEGIN
  INSERT INTO user_facts_fts(user_facts_fts, rowid, fact_text) VALUES ('delete', old.id, old.fact_text);
END;
CREATE TRIGGER IF NOT EXISTS user_facts_au AFTER UPDATE ON user_facts BEGIN
  INSERT INTO user_facts_fts(user_facts_fts, rowid, fact_text) VALUES ('delete', old.id, old.fact_text);
  INSERT INTO user_facts_fts(rowid, fact_text) VALUES (new.id, new.fact_text);
END;

-- triggers: mantener FTS5 sincronizado con la tabla base (lessons)
CREATE TRIGGER IF NOT EXISTS lessons_ai AFTER INSERT ON lessons BEGIN
  INSERT INTO lessons_fts(rowid, lesson_text) VALUES (new.id, new.lesson_text);
END;
CREATE TRIGGER IF NOT EXISTS lessons_ad AFTER DELETE ON lessons BEGIN
  INSERT INTO lessons_fts(lessons_fts, rowid, lesson_text) VALUES ('delete', old.id, old.lesson_text);
END;
CREATE TRIGGER IF NOT EXISTS lessons_au AFTER UPDATE ON lessons BEGIN
  INSERT INTO lessons_fts(lessons_fts, rowid, lesson_text) VALUES ('delete', old.id, old.lesson_text);
  INSERT INTO lessons_fts(rowid, lesson_text) VALUES (new.id, new.lesson_text);
END;

-- triggers: mantener FTS5 sincronizado con la tabla base (image_catalog)
CREATE TRIGGER IF NOT EXISTS image_catalog_ai AFTER INSERT ON image_catalog BEGIN
  INSERT INTO image_catalog_fts(rowid, description, tags, ocr_text)
  VALUES (new.id, new.description, new.tags, new.ocr_text);
END;
CREATE TRIGGER IF NOT EXISTS image_catalog_ad AFTER DELETE ON image_catalog BEGIN
  INSERT INTO image_catalog_fts(image_catalog_fts, rowid, description, tags, ocr_text)
  VALUES ('delete', old.id, old.description, old.tags, old.ocr_text);
END;
CREATE TRIGGER IF NOT EXISTS image_catalog_au AFTER UPDATE ON image_catalog BEGIN
  INSERT INTO image_catalog_fts(image_catalog_fts, rowid, description, tags, ocr_text)
  VALUES ('delete', old.id, old.description, old.tags, old.ocr_text);
  INSERT INTO image_catalog_fts(rowid, description, tags, ocr_text)
  VALUES (new.id, new.description, new.tags, new.ocr_text);
END;
"""


# ─── Conexión e inicialización ─────────────────────────────────────────────
def _load_vec_extension(conn: sqlite3.Connection) -> None:
    """Carga la extensión loadable de sqlite-vec en la conexión."""
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def init_db(path: Path | str = DB_PATH, timeout: float = 5.0) -> sqlite3.Connection:
    """Abre/crea botata.db, aplica pragmas, carga sqlite-vec y crea el esquema.

    Devuelve una conexión lista para uso del bot. `check_same_thread=False`
    porque langgraph puede ejecutar nodos en hilos; WAL cubre la concurrencia.
    `timeout` es cuánto espera un lock ajeno antes de "database is locked":
    un segundo proceso (la config UI editando en vivo) lo sube para aguantar
    las ráfagas de escritura del bot en vez de reventar.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    for pragma in ("journal_mode=WAL", "foreign_keys=ON", "synchronous=NORMAL"):
        conn.execute(f"PRAGMA {pragma}")
    _load_vec_extension(conn)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    log.info("DB ready at %s (sqlite_vec loaded)", path)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Migraciones idempotentes sobre DBs existentes (el _SCHEMA solo crea, no altera).

    - events.done (2026-07-19): marca de cumplimiento para eventos-acción
      (kind='bot_action') — la tarea actions los ejecuta una sola vez.
    - events.recur (2026-07-24): recurrencia ('daily'|'weekly'|'monthly'|'yearly'|NULL).
      event_at define la PRIMERA ocurrencia (y con ella la hora, el día de
      semana o el día del mes según el patrón).
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    if "done" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN done INTEGER NOT NULL DEFAULT 0")
        log.info("migración: events.done agregada")
    if "recur" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN recur TEXT")
        log.info("migración: events.recur agregada")
    if "announced_at" not in cols:
        # 2026-07-26: última ocurrencia ANUNCIADA por la tarea calendar (el
        # calendario actúa siempre; esto garantiza una vez y solo una por
        # ocurrencia). NULL = nunca anunciado.
        conn.execute("ALTER TABLE events ADD COLUMN announced_at TEXT")
        log.info("migración: events.announced_at agregada")
    if "announce" not in cols:
        # 2026-07-26: switch por evento — ¿la tarea calendar lo anuncia? Se fija
        # al CREAR (según la política CALENDAR_ANNOUNCE + quién lo creó) y el
        # admin lo togglea desde la UI. NULL = evento legado o creador bajo
        # política 'groups': se decide a la hora de anunciar (gate viejo).
        conn.execute("ALTER TABLE events ADD COLUMN announce INTEGER")
        log.info("migración: events.announce agregada")

    # bot_memory: compactación (T48, 2026-07-28)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(bot_memory)")}
    if "pinned" not in cols:
        conn.execute("ALTER TABLE bot_memory ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        # La identidad del bot venía del MEMORY.md curado a mano: nace fijada.
        # Lo que escribió la comunidad por tool NO — es justo lo que hay que poder
        # deduplicar (medido en producción: los duplicados y las contradicciones
        # están repartidos entre el admin y los power users por igual).
        n = conn.execute(
            "UPDATE bot_memory SET pinned = 1 WHERE source LIKE 'migration:%'").rowcount
        log.info("migración: bot_memory.pinned agregada (%d fijadas por origen)", n)
    if "superseded_by" not in cols:
        conn.execute("ALTER TABLE bot_memory ADD COLUMN superseded_by INTEGER "
                     "REFERENCES bot_memory(id) ON DELETE SET NULL")
        log.info("migración: bot_memory.superseded_by agregada")

    # user_facts: 📌 (T49c, 2026-07-28). Lo que alguien pidió recordar
    # explícitamente entra siempre y no lo toca la compactación. No se puede
    # inferir para lo ya guardado —"acordate de que soy de Racing" y el bot
    # anotándolo por su cuenta quedaron idénticos—, así que nadie nace fijado:
    # se marca de acá en adelante, y el admin puede fijar a mano lo viejo.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_facts)")}
    if "pinned" not in cols:
        conn.execute("ALTER TABLE user_facts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        log.info("migración: user_facts.pinned agregada")

    # replied_posts.attempts (T55, 2026-08-02): tope de reintentos. Una mención
    # 'failed' se reprocesa en cada poll mientras siga en la ventana de notifs;
    # con los endpoints caídos eso fueron 1h20 de reintentos cada 4 minutos
    # (reejecutando la fase de tools, que ahora cuesta plata). Las que ya
    # existen arrancan en 0: el tope corre de acá en adelante.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(replied_posts)")}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE replied_posts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        log.info("migración: replied_posts.attempts agregada")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(interactions)")}
    if "superseded_by" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN superseded_by INTEGER "
                     "REFERENCES interactions(id) ON DELETE SET NULL")
        log.info("migración: interactions.superseded_by agregada")


# ─── Embeddings (bge-m3, lazy) ─────────────────────────────────────────────
_embedder: Any = None  # SentenceTransformer | None


def get_embedder() -> Any:
    """Carga perezosa del modelo de embeddings (bge-m3).

    Es pesado (~2GB en disco, carga en RAM) y puede no ser necesario en todos
    los procesos (ej. tests de esquema), por eso se difiere al primer uso.
    """
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model %s ...", EMBED_MODEL_NAME)
        _embedder = SentenceTransformer(EMBED_MODEL_NAME)
    return _embedder


def embed(text: str) -> bytes:
    """Embedding denso de `text` como bytes float32 little-endian (para vec0).

    bge-m3 dense no requiere prefijos. La métrica cosine de vec0 normaliza
    implícitamente, así que no se normaliza acá.
    """
    model = get_embedder()
    # show_progress_bar=False: sin esto, sentence-transformers imprime una barra
    # "Batches: 100%|..." POR LLAMADA cuando el logging está en INFO — spam puro
    # para encodes de un solo texto.
    vec = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
    return np.asarray(vec, dtype="float32").tobytes()


# ─── Escritura: upsert + dedup semántico ────────────────────────────────────
def upsert_user_fact(
    conn: sqlite3.Connection,
    handle: str,
    fact_text: str,
    source_uri: str | None = None,
    *,
    threshold: float = DEDUP_THRESHOLD,
    pinned: bool = False,
) -> int | None:
    """Inserta un hecho para `handle` salvo que exista uno semánticamente duplicado.

    El dedup corre solo dentro de la partición del usuario (partition_key=handle):
    busca el vecino más cercano del candidato y, si coseno >= threshold, lo
    considera duplicado y salta el insert (devuelve None). Si no hay duplicado,
    inserta la fila + el vector y devuelve el nuevo id.

    `superseded_by` NO se toca acá: es para merges explícitos posteriores (un
    hecho que reemplaza a otro, ej. "Vive en Rosario" → "Se mudó a Córdoba").
    El dedup automático solo previene duplicados, no mergea.
    """
    q = embed(fact_text)
    hit = conn.execute(
        "SELECT rowid AS id, distance FROM user_facts_vec "
        "WHERE embedding MATCH ? AND k = 1 AND partition_key = ? "
        "ORDER BY distance",
        (q, handle),
    ).fetchone()
    if hit is not None and (1.0 - hit["distance"]) >= threshold:
        # Si lo pidieron explícitamente, el duplicado no se descarta en silencio:
        # se FIJA el que ya estaba. "Acordate de que soy de Racing" tiene que
        # dejar marca aunque el bot ya lo supiera, o el pedido se pierde.
        if pinned:
            conn.execute("UPDATE user_facts SET pinned = 1 WHERE id = ?", (hit["id"],))
            conn.commit()
            log.info("user_facts: ya lo sabía, fijo el existente 📌 (@%s id=%s)",
                     handle, hit["id"])
        else:
            log.debug("user_facts dedup skip (handle=%s sim=%.3f)", handle, 1.0 - hit["distance"])
        return None

    cur = conn.execute(
        "INSERT INTO user_facts(handle, fact_text, source_uri, pinned) VALUES (?, ?, ?, ?)",
        (handle, fact_text, source_uri, 1 if pinned else 0),
    )
    fid = cur.lastrowid
    conn.execute(
        "INSERT INTO user_facts_vec(rowid, embedding, partition_key) VALUES (?, ?, ?)",
        (fid, q, handle),
    )
    conn.commit()
    return fid


def upsert_lesson(
    conn: sqlite3.Connection,
    lesson_text: str,
    scope: str = "community",
    *,
    threshold: float = DEDUP_THRESHOLD,
) -> int | None:
    """Inserta una lección salvo que exista una duplicada dentro del mismo `scope`.

    lessons_vec no está particionada (cross-user), así que el dedup recupera los
    k=10 más cercanos globales y salta el insert si alguno del mismo scope supera
    el threshold. `scope`: 'community' o 'user:<handle>'.
    """
    q = embed(lesson_text)
    hits = conn.execute(
        "SELECT v.rowid AS id, v.distance AS d, l.scope AS scope "
        "FROM lessons_vec v JOIN lessons l ON l.id = v.rowid "
        "WHERE v.embedding MATCH ? AND k = 10 ORDER BY v.distance",
        (q,),
    ).fetchall()
    for hit in hits:
        if hit["scope"] == scope and (1.0 - hit["d"]) >= threshold:
            log.debug("lessons dedup skip (scope=%s sim=%.3f)", scope, 1.0 - hit["d"])
            return None

    cur = conn.execute(
        "INSERT INTO lessons(lesson_text, scope) VALUES (?, ?)",
        (lesson_text, scope),
    )
    lid = cur.lastrowid
    conn.execute(
        "INSERT INTO lessons_vec(rowid, embedding) VALUES (?, ?)",
        (lid, q),
    )
    conn.commit()
    return lid


# ─── Lectura: búsqueda híbrida (vec + FTS5 + RRF) ───────────────────────────
def _rrf(rankings: list[list[int]], *, k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: fusiona listas rankeadas de rowids.

    score(d) = Σ 1 / (k + rank)  sobre cada lista donde d aparece (rank 1-based).
    Devuelve [(rowid, score), ...] ordenado por score descendente.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _fts_query(text: str) -> str:
    """Sanitiza `text` para FTS5: quita todo menos palabras y las cita como frases.

    Evita errores de sintaxis de FTS5 con puntuación/operadores y mantiene los
    acentos (re.UNICODE). Ej: "Vive en Rosario!" → '"Vive" "en" "Rosario"'.
    """
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    return " ".join(f'"{t}"' for t in tokens)


def hybrid_search_user_facts(
    conn: sqlite3.Connection,
    handle: str,
    query: str,
    k: int = 5,
    *,
    rrf_k: int = 60,
) -> list[tuple[int, str]]:
    """Recupera los k hechos más relevantes de `handle` para `query`.

    Búsqueda híbrida: vec0 (coseno, partición del usuario) + FTS5 (BM25, filtrado
    por handle en el JOIN), fusionadas por RRF. Devuelve [(id, fact_text), ...].

    Los hechos archivados (`superseded_by`) quedan afuera de las DOS ramas. Sin
    ese filtro, compactar sería peor que no compactar: el hecho fusionado se
    sumaría a los originales en vez de reemplazarlos.

    Los 📌 tampoco entran acá: van SIEMPRE y por separado
    (`pinned_user_facts`). Si compitieran por los k lugares, fijar un hecho
    podría dejar afuera otro relevante — y encima el que alguien pidió recordar
    quedaría sujeto a que la búsqueda lo encuentre, que es justo lo que fijarlo
    viene a evitar.
    """
    q = embed(query)
    pool = max(k * 3, 15)

    vec_rows = conn.execute(
        "SELECT v.rowid AS id, f.fact_text AS text "
        "FROM user_facts_vec v JOIN user_facts f ON f.id = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ? AND v.partition_key = ? "
        "AND f.superseded_by IS NULL AND f.pinned = 0 "
        "ORDER BY v.distance",
        (q, pool, handle),
    ).fetchall()
    fts_rows = conn.execute(
        "SELECT f.id AS id, f.fact_text AS text "
        "FROM user_facts_fts JOIN user_facts f ON f.id = user_facts_fts.rowid "
        "WHERE user_facts_fts MATCH ? AND f.handle = ? AND f.superseded_by IS NULL "
        "AND f.pinned = 0 "
        "ORDER BY rank LIMIT ?",
        (_fts_query(query), handle, pool),
    ).fetchall()

    fused = _rrf(
        [[r["id"] for r in vec_rows], [r["id"] for r in fts_rows]], k=rrf_k
    )
    by_id = {r["id"]: r["text"] for r in vec_rows}
    by_id.update({r["id"]: r["text"] for r in fts_rows})
    return [(doc_id, by_id[doc_id]) for doc_id, _ in fused[:k]]


def hybrid_search_lessons(
    conn: sqlite3.Connection,
    query: str,
    k: int = 5,
    *,
    rrf_k: int = 60,
) -> list[tuple[int, str]]:
    """Recupera las k lecciones más relevantes para `query` (cross-user).

    vec0 (coseno, global) + FTS5 (BM25), fusionadas por RRF.
    Devuelve [(id, lesson_text), ...].
    """
    q = embed(query)
    pool = max(k * 3, 15)

    vec_rows = conn.execute(
        "SELECT v.rowid AS id, l.lesson_text AS text "
        "FROM lessons_vec v JOIN lessons l ON l.id = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (q, pool),
    ).fetchall()
    fts_rows = conn.execute(
        "SELECT l.id AS id, l.lesson_text AS text "
        "FROM lessons_fts JOIN lessons l ON l.id = lessons_fts.rowid "
        "WHERE lessons_fts MATCH ? ORDER BY rank LIMIT ?",
        (_fts_query(query), pool),
    ).fetchall()

    fused = _rrf(
        [[r["id"] for r in vec_rows], [r["id"] for r in fts_rows]], k=rrf_k
    )
    by_id = {r["id"]: r["text"] for r in vec_rows}
    by_id.update({r["id"]: r["text"] for r in fts_rows})
    return [(doc_id, by_id[doc_id]) for doc_id, _ in fused[:k]]


# ─── clearsky_cache: proxy de "quién me bloquea" ────────────────────────────
# TTL corto (1h): los bloques no cambian seguido y amortizamos repetición del
# comando /bloques. El JSON guarda la lista cruda de {did, blocked_date}.
CLEARSKY_CACHE_TTL_SECONDS = 3600


def get_cached_blocklist(
    conn: sqlite3.Connection, did: str, *, ttl_seconds: int = CLEARSKY_CACHE_TTL_SECONDS
) -> list[dict[str, str]] | None:
    """Devuelve la lista cacheada de bloqueadores de `did`, o None si no hay /
    expiró. None (no []) para que la caller sepa que debe refrescar contra ClearSky.
    """
    row = conn.execute(
        "SELECT blockers_json, fetched_at FROM clearsky_cache WHERE did = ?", (did,)
    ).fetchone()
    if row is None:
        return None

    fetched_at = datetime.fromisoformat(row["fetched_at"]) if "T" in row["fetched_at"] else None
    if fetched_at is not None and (datetime.now(timezone.utc) - fetched_at) > timedelta(seconds=ttl_seconds):
        return None  # stale → forzar refresco
    return json.loads(row["blockers_json"])


def save_blocklist_cache(
    conn: sqlite3.Connection, did: str, blockers: list[dict[str, str]]
) -> None:
    """Persiste la lista de bloqueadores para `did` con timestamp actual."""
    conn.execute(
        "INSERT OR REPLACE INTO clearsky_cache (did, blockers_json, fetched_at) "
        "VALUES (?, ?, ?)",
        (did, json.dumps(blockers, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ─── image_catalog: mapa de conocimiento de imágenes ─────────────────────────
def _build_image_embed_text(category: str, description: str, tags: str, ocr_text: str | None) -> str:
    """Arma el texto que se embeebe para búsqueda semántica de imágenes."""
    return f"[{category}] {description} | tags: {tags} | texto visible: {ocr_text or ''}"


def upsert_image_catalog(
    conn: sqlite3.Connection,
    *,
    platform: str,
    external_id: str,
    source_name: str,
    file_path: str,
    description: str,
    category: str,
    tags: list[str] | str,
    ocr_text: str | None,
    source_url: str | None,
    posted_at: str | None,
) -> int:
    """Inserta o actualiza un ítem en el catálogo de imágenes.

    Dedup por (platform, external_id, file_path). Si ya existe, actualiza
    description/tags/category y re-embebe. Devuelve el id de la fila.
    """
    tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else (tags or "[]")

    row = conn.execute(
        "SELECT id FROM image_catalog WHERE platform = ? AND external_id = ? AND file_path = ?",
        (platform, external_id, file_path),
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE image_catalog SET description = ?, category = ?, tags = ?, ocr_text = ?, "
            "source_name = ?, source_url = ?, posted_at = ? WHERE id = ?",
            (description, category, tags_json, ocr_text, source_name, source_url, posted_at, row["id"]),
        )
        fid = row["id"]
        # Re-embed: delete old vector, insert new
        conn.execute("DELETE FROM image_catalog_vec WHERE rowid = ?", (fid,))
    else:
        cur = conn.execute(
            "INSERT INTO image_catalog "
            "(platform, external_id, source_name, file_path, description, category, tags, ocr_text, source_url, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, external_id, source_name, file_path, description, category,
             tags_json, ocr_text, source_url, posted_at),
        )
        fid = cur.lastrowid

    search_text = _build_image_embed_text(category, description, tags_json, ocr_text)
    q = embed(search_text)
    conn.execute("INSERT INTO image_catalog_vec(rowid, embedding) VALUES (?, ?)", (fid, q))
    conn.commit()
    return fid


def hybrid_search_image_catalog(
    conn: sqlite3.Connection,
    query: str,
    *,
    category: str | None = None,
    sources: list[str] | None = None,
    limit: int = 5,
    rrf_k: int = 60,
) -> list[dict]:
    """Recupera las `limit` imágenes más relevantes para `query`.

    Búsqueda híbrida (vec0 + FTS5 + RRF). Filtros opcionales:
    - `category`: tipo de archivo ('meme' | 'foto' | 'arte' | 'captura' | 'otro').
    - `sources`: lista de `source_name` (cuentas/boards de origen). Es el filtro
      TEMÁTICO (T38): el registro `content_sources.json` traduce un tema
      ("fútbol") al conjunto de fuentes que el admin declaró para ese tema, y
      acá se aplica. Lista vacía = ninguna fuente declarada → sin resultados
      (mejor devolver nada que ignorar el filtro y postear cualquier cosa).

    Devuelve lista de dicts con todos los campos de image_catalog.
    """
    if sources is not None and not sources:
        return []
    q = embed(query)
    pool = max(limit * 3, 15)

    # WHERE dinámico: los filtros se combinan (se aplican sobre la tabla base `i`,
    # por eso la rama vectorial también joinea cuando hay alguno).
    conds, params = [], []
    if category:
        conds.append("i.category = ?")
        params.append(category)
    if sources:
        conds.append(f"i.source_name IN ({','.join('?' for _ in sources)})")
        params.extend(sources)
    extra = (" AND " + " AND ".join(conds)) if conds else ""

    if conds:
        vec_rows = conn.execute(
            "SELECT v.rowid AS id, v.distance AS distance FROM image_catalog_vec v "
            "JOIN image_catalog i ON i.id = v.rowid "
            f"WHERE v.embedding MATCH ? AND k = ?{extra} "
            "ORDER BY v.distance",
            (q, pool, *params),
        ).fetchall()
    else:
        vec_rows = conn.execute(
            "SELECT v.rowid AS id, v.distance AS distance FROM image_catalog_vec v "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (q, pool),
        ).fetchall()
    fts_rows = conn.execute(
        "SELECT i.id AS id FROM image_catalog_fts "
        "JOIN image_catalog i ON i.id = image_catalog_fts.rowid "
        f"WHERE image_catalog_fts MATCH ?{extra} "
        "ORDER BY rank LIMIT ?",
        (_fts_query(query), *params, pool),
    ).fetchall()

    if not vec_rows and not fts_rows:
        return []

    fused = _rrf(
        [[r["id"] for r in vec_rows], [r["id"] for r in fts_rows]], k=rrf_k
    )
    if not fused:
        return []

    ids = [doc_id for doc_id, _ in fused[:limit]]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM image_catalog WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    # `vec_distance` es la señal de RELEVANCIA para el que llama. El score de RRF
    # no sirve para eso: es rank-fusion, el primero siempre saca ~2/61 aunque no
    # tenga nada que ver con la query. La distancia coseno sí mide parecido. Sin
    # esto no hay forma de distinguir "el catálogo tiene esto" de "esto es lo
    # menos lejano que encontré", que es como un pedido de mapaches terminaba
    # devolviendo un carpincho. None = el candidato salió solo por FTS.
    dist = {r["id"]: r["distance"] for r in vec_rows}
    return [{**by_id[doc_id], "vec_distance": dist.get(doc_id)}
            for doc_id, _score in fused[:limit] if doc_id in by_id]


def list_uncataloged_files(
    conn: sqlite3.Connection, base_dir: Path
) -> list[tuple[str, str, str]]:
    """Devuelve [(platform, external_id, file_path), ...] de archivos en disco
    que no tienen fila en image_catalog.

    Escanea scrape/pictures/<platform>/ recursivamente.
    """
    uncataloged: list[tuple[str, str, str]] = []
    pictures_dir = base_dir / "scrape" / "pictures"
    if not pictures_dir.exists():
        return uncataloged

    for platform_dir in sorted(pictures_dir.iterdir()):
        if not platform_dir.is_dir():
            continue
        platform = platform_dir.name

        for img_file in sorted(platform_dir.iterdir()):
            if img_file.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue

            file_path = str(img_file.relative_to(base_dir)).replace("\\", "/")

            # Parsear external_id del nombre. Dos sufijos que deja Membrilla:
            #   <external_id>_<n>.jpg    → media n (foto simple / slide de carrusel)
            #   <external_id>_f<n>.jpg   → frame n de un video (TikTok)
            # Ambos resuelven al mismo <external_id> base, que es el nombre del
            # sidecar <external_id>.json. Sin esto, los frames de TikTok no
            # encuentran su sidecar y caen a source_name='manual'.
            stem = img_file.stem
            parts = stem.rsplit("_", 1)
            suffix = parts[1] if len(parts) == 2 else ""
            is_media = suffix.isdigit()
            is_frame = suffix.startswith("f") and suffix[1:].isdigit()
            if len(parts) == 2 and (is_media or is_frame):
                external_id = parts[0]
            else:
                external_id = stem  # fallback: nombre completo sin extensión

            row = conn.execute(
                "SELECT 1 FROM image_catalog WHERE platform = ? AND external_id = ? AND file_path = ?",
                (platform, external_id, file_path),
            ).fetchone()
            if not row:
                uncataloged.append((platform, external_id, file_path))

    return uncataloged


def get_image_catalog_stats(conn: sqlite3.Connection) -> dict:
    """Resumen del catálogo por categoría y fuente (para el contexto del bot)."""
    rows = conn.execute(
        "SELECT category, source_name, platform, COUNT(*) AS cnt "
        "FROM image_catalog GROUP BY category, source_name, platform "
        "ORDER BY category, cnt DESC"
    ).fetchall()
    return {
        "total": conn.execute("SELECT COUNT(*) FROM image_catalog").fetchone()[0],
        "by_category": [
            {"category": r["category"], "source_name": r["source_name"],
             "platform": r["platform"], "count": r["cnt"]}
            for r in rows
        ],
    }


def mark_image_used(conn: sqlite3.Connection, image_id: int) -> None:
    """Marca una imagen como usada (actualiza used_at y use_count)."""
    conn.execute(
        "UPDATE image_catalog SET used_at = datetime('now'), use_count = use_count + 1 "
        "WHERE id = ?",
        (image_id,),
    )
    conn.commit()


MEDIA_REUSE_COOLDOWN_H = 24 * 14  # dos semanas sin repetir un medio, si hay alternativa


def prefer_fresh_media(
    results: list[dict],
    *,
    cooldown_hours: int = MEDIA_REUSE_COOLDOWN_H,
    now: datetime | None = None,
) -> list[dict]:
    """Reordena candidatos del catálogo para no repetir el mismo medio.

    Los nunca usados (o usados antes del cooldown) van primero, en su orden de
    relevancia; los usados dentro del cooldown van al fondo, del más viejo al
    más nuevo. Con catálogo chico degrada con dignidad: si TODO está en
    cooldown, gana el menos recientemente usado (nunca el de recién).
    `used_at` se guarda con datetime('now') (UTC) — se compara como string.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=cooldown_hours)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = [r for r in results if not r.get("used_at") or r["used_at"] < cutoff]
    stale = sorted(
        (r for r in results if r.get("used_at") and r["used_at"] >= cutoff),
        key=lambda r: r["used_at"],
    )
    return fresh + stale


# ─── Events / calendario (T4) ──────────────────────────────────────────────
# event_at se guarda en hora LOCAL de la instancia (ISO 8601, naive — ver
# LOCAL_TZ / settings TIMEZONE). Las queries usan por default "ahora"/"hoy" en
# esa zona; pasá `now`/`day` explícito para testear.
_EVENT_FIELDS = ("handle", "title", "description", "event_at", "kind", "source", "recur")


def create_event(
    conn: sqlite3.Connection,
    *,
    title: str,
    event_at: str,
    handle: str | None = None,
    description: str | None = None,
    kind: str = "other",
    source: str | None = None,
    recur: str | None = None,
    announce: bool | None = None,
) -> int:
    """Crea un evento. Devuelve su id. handle=None → evento de comunidad.

    `recur`: None (una vez) | 'daily' | 'weekly' | 'monthly' | 'yearly'. En los recurrentes,
    event_at es la PRIMERA ocurrencia: fija la hora y — según el patrón — el día
    de semana o del mes. (bot_action recurrente no está soportado: el done es
    único por evento.)

    `announce`: ¿la tarea calendar anuncia este evento? True/False lo fija el
    creador (política CALENDAR_ANNOUNCE evaluada al crear; el admin lo togglea
    después desde la UI). None = se decide a la hora de anunciar (gate viejo).
    """
    if recur not in (None, "daily", "weekly", "monthly", "yearly"):
        raise ValueError(f"recur inválido: {recur!r}")
    cur = conn.execute(
        "INSERT INTO events (handle, title, description, event_at, kind, source, recur, announce) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (handle, title, description, event_at, kind, source, recur,
         None if announce is None else int(announce)),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_event_announce(conn: sqlite3.Connection, event_id: int, announce: bool) -> None:
    """Prende/apaga el anuncio automático de un evento (switch del admin en la UI)."""
    conn.execute("UPDATE events SET announce = ? WHERE id = ?",
                 (int(announce), event_id))
    conn.commit()


def get_event(conn: sqlite3.Connection, event_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def event_exists(
    conn: sqlite3.Connection, *, title: str, event_at: str, handle: str | None = None
) -> bool:
    """True si ya hay un evento del mismo día, mismo dueño y título equivalente.

    Dedup idempotente para el aprendizaje del feed (T6). `handle IS ?` matchea
    tanto NULL (comunidad) como un handle concreto.
    """
    day  = event_at[:10]  # parte fecha del ISO
    rows = conn.execute(
        "SELECT title FROM events WHERE date(event_at) = ? AND handle IS ?",
        (day, handle),
    ).fetchall()
    norm = " ".join(title.lower().split())
    return any(" ".join(r[0].lower().split()) == norm for r in rows)


def user_exists(conn: sqlite3.Connection, handle: str) -> bool:
    """True si el handle ya tiene perfil en users (gate del aprendizaje del feed, T6)."""
    return conn.execute(
        "SELECT 1 FROM users WHERE handle = ?", (handle,)
    ).fetchone() is not None


def update_event(conn: sqlite3.Connection, event_id: int, **fields: Any) -> bool:
    """Actualiza campos de un evento. Devuelve True si existía. Ignora claves desconocidas."""
    cols = [(k, v) for k, v in fields.items() if k in _EVENT_FIELDS]
    if not cols:
        return get_event(conn, event_id) is not None
    set_clause = ", ".join(f"{k} = ?" for k, _ in cols)
    cur = conn.execute(
        f"UPDATE events SET {set_clause} WHERE id = ?",
        (*[v for _, v in cols], event_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_event(conn: sqlite3.Connection, event_id: int) -> bool:
    cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    return cur.rowcount > 0


def _next_occurrence(event_at: str, recur: str, now_dt: datetime) -> datetime | None:
    """Próxima ocurrencia (>= now) de un evento recurrente cuya primera vez fue
    `event_at`. daily = todos los días; weekly = mismo día de semana; monthly =
    mismo día del mes (los meses sin ese día se saltean); yearly = misma fecha
    cada año (29/2 solo en bisiestos)."""
    try:
        first = datetime.fromisoformat(event_at)
    except ValueError:
        return None
    if first >= now_dt:
        return first
    t = first.time()
    if recur == "yearly":
        for y in range(now_dt.year, now_dt.year + 5):  # +5: cubre 29/2
            try:
                cand = datetime(y, first.month, first.day, t.hour, t.minute, t.second)
            except ValueError:  # 29/2 en año no bisiesto
                continue
            if cand >= now_dt:
                return cand
        return None
    if recur == "daily":
        cand = datetime.combine(now_dt.date(), t)
        return cand if cand >= now_dt else cand + timedelta(days=1)
    if recur == "weekly":
        ahead = (first.weekday() - now_dt.weekday()) % 7
        cand = datetime.combine(now_dt.date() + timedelta(days=ahead), t)
        return cand if cand >= now_dt else cand + timedelta(days=7)
    if recur == "monthly":
        y, m = now_dt.year, now_dt.month
        for _ in range(13):
            try:
                cand = datetime(y, m, first.day, t.hour, t.minute, t.second)
            except ValueError:  # ese mes no tiene el día (ej. 31)
                cand = None
            if cand is not None and cand >= now_dt:
                return cand
            m += 1
            if m > 12:
                m, y = 1, y + 1
    return None


def upcoming_events(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    limit: int = 10,
    handle: str | None = None,
) -> list[dict]:
    """Próximos `limit` eventos, ascendente: los puntuales con event_at >= ahora
    + los recurrentes con su PRÓXIMA ocurrencia calculada (el dict devuelto trae
    `event_at` ya movido a esa ocurrencia; el campo `recur` distingue).

    Si `handle` se pasa → eventos de ese usuario + los de comunidad (handle IS NULL).
    Si es None → todos. `now` default = ahora en la zona local (LOCAL_TZ).
    """
    if now is None:
        now = local_now().isoformat(timespec="minutes")
    now_expr = "datetime(replace(?,'T',' '))"
    params: list[Any] = [now]
    # replace(): event_at ISO usa 'T', datetime() de SQLite usa espacio — sin
    # normalizar, la comparación de strings miente (ver due_bot_actions).
    where = [f"(datetime(replace(event_at,'T',' ')) >= {now_expr} OR recur IS NOT NULL)"]
    if handle is not None:
        where.append("(handle = ? OR handle IS NULL)")
        params.append(handle)
    rows = conn.execute(
        f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY event_at ASC",
        tuple(params),
    ).fetchall()
    now_dt = datetime.fromisoformat(now)
    out: list[dict] = []
    for r in rows:
        ev = dict(r)
        if ev.get("recur"):
            nxt = _next_occurrence(ev["event_at"], ev["recur"], now_dt)
            if nxt is None:
                continue
            ev["event_at"] = nxt.isoformat(timespec="minutes" if nxt.second == 0 else "seconds")
        out.append(ev)
    out.sort(key=lambda e: e["event_at"])
    return out[:limit]


def events_today(
    conn: sqlite3.Connection,
    *,
    day: str | None = None,
    handle: str | None = None,
) -> list[dict]:
    """Eventos cuyo event_at cae en `day` (YYYY-MM-DD) + los recurrentes cuya
    pauta matchea ese día (daily siempre; weekly mismo día de semana; monthly
    mismo día del mes — siempre desde su primera ocurrencia en adelante).
    Default = hoy en la zona local (LOCAL_TZ)."""
    if day is None:
        day = local_now().date().isoformat()
    day_expr = "?"
    hits = (f"(date(event_at) = {day_expr} OR (date(event_at) <= {day_expr} AND ("
            "(recur = 'daily') OR "
            f"(recur = 'weekly' AND strftime('%w', event_at) = strftime('%w', {day_expr})) OR "
            f"(recur = 'monthly' AND strftime('%d', event_at) = strftime('%d', {day_expr})) OR "
            f"(recur = 'yearly' AND strftime('%m-%d', event_at) = strftime('%m-%d', {day_expr})))))")
    params: list[Any] = [day] * 5  # una copia por placeholder usado
    where = [hits]
    if handle is not None:
        where.append("(handle = ? OR handle IS NULL)")
        params.append(handle)
    rows = conn.execute(
        f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY event_at ASC",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    """Lee un valor del estado misceláneo (tabla kv)."""
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert de un valor en el estado misceláneo (tabla kv)."""
    conn.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')",
        (key, value),
    )
    conn.commit()


def kv_del(conn: sqlite3.Connection, key: str) -> None:
    """Borra una clave del estado misceláneo (tabla kv). No-op si no existe."""
    conn.execute("DELETE FROM kv WHERE key = ?", (key,))
    conn.commit()


def _norm_text(text: str) -> str:
    return " ".join(text.lower().split())


def add_bot_memory(conn: sqlite3.Connection, text: str, *,
                   source: str | None = None,
                   created_at: str | None = None,
                   pinned: bool = False) -> int | None:
    """Agrega una entrada a la memoria general del bot. Devuelve su id,
    o None si ya existía una entrada con el mismo texto normalizado (dedup).

    `pinned` = el pase de compactación no la va a tocar (la pidió el admin, o
    alguien dijo explícitamente "acordate de esto")."""
    norm = _norm_text(text)
    for row in conn.execute("SELECT id, text FROM bot_memory "
                            "WHERE superseded_by IS NULL").fetchall():
        if _norm_text(row["text"]) == norm:
            return None
    if created_at:
        cur = conn.execute(
            "INSERT INTO bot_memory (text, source, created_at, pinned) VALUES (?, ?, ?, ?)",
            (text, source, created_at, int(pinned)))
    else:
        cur = conn.execute(
            "INSERT INTO bot_memory (text, source, pinned) VALUES (?, ?, ?)",
            (text, source, int(pinned)))
    conn.commit()
    return int(cur.lastrowid)


def list_bot_memory(conn: sqlite3.Connection, limit: int = 200, *,
                    incluir_archivadas: bool = False) -> list[dict]:
    """Memoria general del bot, cronológica (más vieja primero — lectura natural).

    Por defecto SOLO las vigentes: lo compactado sigue en la tabla para poder
    auditarlo o revertirlo, pero no vuelve a entrar al contexto."""
    filtro = "" if incluir_archivadas else "WHERE superseded_by IS NULL"
    rows = conn.execute(
        f"SELECT * FROM bot_memory {filtro} ORDER BY created_at ASC, id ASC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_bot_memory_pinned(conn: sqlite3.Connection, mem_id: int, pinned: bool) -> bool:
    cur = conn.execute("UPDATE bot_memory SET pinned = ? WHERE id = ?",
                       (int(pinned), mem_id))
    conn.commit()
    return cur.rowcount > 0


def supersede_bot_memory(conn: sqlite3.Connection, viejas: list[int],
                         sucesora: int | None) -> None:
    """Archiva `viejas` apuntando a `sucesora` (o a sí mismas si se descartaron).

    No borra: el texto original queda para auditar qué se perdió y para poder
    revertir. `restore_bot_memory` deshace."""
    for vid in viejas:
        conn.execute("UPDATE bot_memory SET superseded_by = ? WHERE id = ?",
                     (sucesora if sucesora is not None else vid, vid))
    conn.commit()


def restore_bot_memory(conn: sqlite3.Connection, mem_id: int) -> bool:
    """Devuelve al contexto una fila archivada (undo de la compactación)."""
    cur = conn.execute("UPDATE bot_memory SET superseded_by = NULL WHERE id = ?", (mem_id,))
    conn.commit()
    return cur.rowcount > 0


def delete_bot_memory(conn: sqlite3.Connection, mem_id: int) -> bool:
    cur = conn.execute("DELETE FROM bot_memory WHERE id = ?", (mem_id,))
    conn.commit()
    return cur.rowcount > 0


def add_preference(conn: sqlite3.Connection, kind: str, text: str, *,
                   source: str = "bot") -> int | None:
    """Agrega un gusto/disgusto. Devuelve su id, o None si ya existía uno
    equivalente (dedup por texto normalizado, cross-kind: un mismo texto no
    puede ser gusto Y disgusto a la vez)."""
    if kind not in ("like", "dislike"):
        raise ValueError(f"kind inválido: {kind!r} (like|dislike)")
    norm = _norm_text(text)
    for row in conn.execute("SELECT text FROM preferences").fetchall():
        if _norm_text(row["text"]) == norm:
            return None
    cur = conn.execute(
        "INSERT INTO preferences (kind, text, source) VALUES (?, ?, ?)",
        (kind, text, source))
    conn.commit()
    return int(cur.lastrowid)


def list_preferences(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM preferences ORDER BY kind, created_at ASC, id ASC").fetchall()
    return [dict(r) for r in rows]


def find_preference(conn: sqlite3.Connection, text: str) -> dict | None:
    """Busca una preferencia por texto normalizado (para remove por texto)."""
    norm = _norm_text(text)
    for row in conn.execute("SELECT * FROM preferences").fetchall():
        if _norm_text(row["text"]) == norm:
            return dict(row)
    return None


def delete_preference(conn: sqlite3.Connection, pref_id: int) -> bool:
    cur = conn.execute("DELETE FROM preferences WHERE id = ?", (pref_id,))
    conn.commit()
    return cur.rowcount > 0


def due_bot_actions(conn: sqlite3.Connection, *, now: str | None = None) -> list[dict]:
    """Eventos-acción (kind='bot_action') vencidos y no cumplidos: event_at <= ahora.

    Son órdenes agendadas para el bot ("posteá X a tal hora"), no contexto: el
    la tarea `actions` las ejecuta en el primer ciclo después de la hora y las
    marca done. `now` default = ahora en la zona local (LOCAL_TZ).
    """
    if now is None:
        now = local_now().isoformat(timespec="minutes")
    now_expr = "datetime(replace(?,'T',' '))"
    params: tuple = (now,)
    # datetime(replace(...)) normaliza el separador: event_at se guarda ISO con 'T'
    # pero datetime('now') devuelve con espacio, y en comparación de strings
    # 'T' > ' ' haría que NINGÚN evento del día aparezca como vencido.
    rows = conn.execute(
        f"SELECT * FROM events WHERE kind = 'bot_action' AND done = 0 "
        f"AND datetime(replace(event_at,'T',' ')) <= {now_expr} ORDER BY event_at ASC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def mark_event_done(conn: sqlite3.Connection, event_id: int) -> None:
    """Marca un evento-acción como cumplido (no se vuelve a ejecutar)."""
    conn.execute("UPDATE events SET done = 1 WHERE id = ?", (event_id,))
    conn.commit()


def due_calendar_announcements(conn: sqlite3.Connection, *, now: str | None = None,
                               grace_hours: int = 24) -> list[dict]:
    """Eventos comunes (kind != 'bot_action') cuya ocurrencia venció y NO fue
    anunciada: el motor de "el calendario actúa SIEMPRE" (tarea calendar).

    Para recurrentes se calcula la ocurrencia más reciente dentro de la ventana
    de gracia. `grace_hours` acota lo perdido: si el bot estuvo caído más que
    eso, el anuncio ya no tiene sentido y la ocurrencia se saltea (queda para
    la próxima). Cada dict sale con `occurrence` (ISO) para marcarla anunciada.
    """
    now_dt = datetime.fromisoformat(now) if now is not None else local_now()
    floor = now_dt - timedelta(hours=grace_hours)
    rows = conn.execute("SELECT * FROM events WHERE kind != 'bot_action'").fetchall()
    out: list[dict] = []
    for r in rows:
        ev = dict(r)
        if ev.get("recur"):
            # ocurrencia más RECIENTE dentro de [piso, ahora]: si hubiera varias
            # vencidas en la ventana, la última es la que vale anunciar.
            occ = _next_occurrence(ev["event_at"], ev["recur"], now_dt=floor)
            while occ is not None and occ <= now_dt:
                nxt = _next_occurrence(ev["event_at"], ev["recur"],
                                       now_dt=occ + timedelta(minutes=1))
                if nxt is None or nxt > now_dt:
                    break
                occ = nxt
            if occ is None:
                continue
        else:
            try:
                occ = datetime.fromisoformat(ev["event_at"])
            except ValueError:
                continue
            if occ < floor:
                continue  # más viejo que la gracia: anunciar tarde ya no suma
        if occ > now_dt:
            continue  # todavía no venció
        occ_iso = occ.isoformat(timespec="minutes" if occ.second == 0 else "seconds")
        if ev.get("announced_at") and ev["announced_at"] >= occ_iso:
            continue  # esta ocurrencia ya se anunció
        ev["occurrence"] = occ_iso
        out.append(ev)
    out.sort(key=lambda e: e["occurrence"])
    return out


def mark_event_announced(conn: sqlite3.Connection, event_id: int,
                         occurrence: str) -> None:
    """Registra que la ocurrencia `occurrence` (ISO) ya fue anunciada."""
    conn.execute("UPDATE events SET announced_at = ? WHERE id = ?",
                 (occurrence, event_id))
    conn.commit()


# ─── relationships: grafo ponderado entre usuarios ──────────────────────────
def bump_relationship(
    conn: sqlite3.Connection,
    handle_a: str,
    handle_b: str,
    kind: str = "thread",
    *,
    inc: float = 1.0,
) -> bool:
    """Incrementa (o crea) la arista no dirigida entre dos usuarios.

    Orden canónico (min, max) para que (a,b) y (b,a) sean la MISMA arista.
    Solo entre usuarios que ya existen en `users` (no crea filas: el grafo es
    entre gente conocida, no un censo). Devuelve True si tocó la arista.
    """
    a, b = sorted((handle_a.strip(), handle_b.strip()))
    if not a or a == b:
        return False
    exists = conn.execute(
        "SELECT COUNT(*) FROM users WHERE handle IN (?, ?)", (a, b)
    ).fetchone()[0]
    if exists < 2:
        return False
    conn.execute(
        "INSERT INTO relationships(handle_a, handle_b, kind, weight) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(handle_a, handle_b, kind) DO UPDATE SET "
        "weight = weight + excluded.weight, last_at = datetime('now')",
        (a, b, kind, inc),
    )
    conn.commit()
    return True


# ─── interactions: log conversacional por usuario ───────────────────────────
def log_interaction(
    conn: sqlite3.Connection,
    handle: str,
    summary: str,
    *,
    source_uri: str | None = None,
    created_at: str | None = None,
) -> int:
    """Registra una nota de interacción directa con un usuario.

    `created_at` explícito solo para reconstruir historial (tests, migraciones):
    en el uso normal lo pone la base."""
    if created_at:
        cur = conn.execute(
            "INSERT INTO interactions(handle, summary, source_uri, created_at) "
            "VALUES (?, ?, ?, ?)", (handle, summary, source_uri, created_at))
    else:
        cur = conn.execute(
            "INSERT INTO interactions(handle, summary, source_uri) VALUES (?, ?, ?)",
            (handle, summary, source_uri))
    conn.commit()
    return cur.lastrowid


def recent_interactions(conn: sqlite3.Connection, handle: str, limit: int = 5) -> list[dict]:
    """Últimas notas de interacción con un usuario, de más nueva a más vieja.

    Recuperación cronológica a propósito (recencia > semántica): es historial
    de conversación, no conocimiento — por eso no tiene embeddings.

    Solo las vigentes: una charla larga deja muchas notas casi iguales y, sin
    compactar, las 5 más recientes terminaban siendo cinco ángulos del mismo
    rato (medido: un usuario con 64 filas de un solo día)."""
    rows = conn.execute(
        "SELECT summary, source_uri, created_at FROM interactions "
        "WHERE handle = ? AND superseded_by IS NULL ORDER BY id DESC LIMIT ?",
        (handle, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_interactions_all(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Últimas notas de interacción de CUALQUIER usuario, de más nueva a más vieja.

    Señal de "clima de la comunidad": cómo viene tratando la gente al bot en las
    conversaciones recientes (insultos, buena onda, temas). La usa el pase de
    mood en modo auto. Cronológico (recencia), sin embeddings."""
    rows = conn.execute(
        "SELECT handle, summary, created_at FROM interactions "
        "WHERE superseded_by IS NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def interacciones_compactables(conn: sqlite3.Connection, *,
                               min_por_dia: int = 3) -> list[dict]:
    """Un grupo por USUARIO, con todos sus días compactables adentro.

    El día sigue siendo la unidad de compresión (una nota por día), pero el
    grupo es la persona: el schema devuelve una lista de días, así que una sola
    llamada resuelve todos los días de un usuario. Medido en el backfill: 38
    llamadas pasan a ~15, y en régimen es menos tiempo del loop del bot ocupado.

    Excluye el día MÁS RECIENTE de cada usuario: esa charla puede seguir, y
    resumirla a mitad de camino perdería lo más fresco justo cuando más importa.
    Los días con pocas notas se dejan como están — no hay nada que fusionar.
    """
    filas = conn.execute(
        "SELECT id, handle, summary, source_uri, created_at, date(created_at) AS dia "
        "FROM interactions WHERE superseded_by IS NULL ORDER BY handle, id"
    ).fetchall()
    por_dia: dict[tuple[str, str], list[dict]] = {}
    ultimo_dia: dict[str, str] = {}
    for r in filas:
        por_dia.setdefault((r["handle"], r["dia"]), []).append(dict(r))
        ultimo_dia[r["handle"]] = max(ultimo_dia.get(r["handle"], ""), r["dia"])
    por_handle: dict[str, list[dict]] = {}
    for (h, dia), fs in sorted(por_dia.items()):
        # Un día hecho SOLO de notas que ya salieron de una compactación no
        # vuelve a entrar. El prompt permite hasta tres notas por día cuando el
        # día tuvo charlas separadas, así que la salida del pase puede volver a
        # cumplir el mínimo y el pase se comería su propio resultado: resumir un
        # resumen, y otra vez, hasta dejar la nota en nada. Con notas nuevas
        # mezcladas sí se rehace: ahí hay material que todavía no se resumió.
        if all(f["source_uri"] == "compact" for f in fs):
            continue
        if len(fs) >= min_por_dia and dia != ultimo_dia[h]:
            por_handle.setdefault(h, []).append({"dia": dia, "filas": fs})
    return [{"handle": h, "dias": ds, "filas": [f for d in ds for f in d["filas"]]}
            for h, ds in sorted(por_handle.items())]


def supersede_interactions(conn: sqlite3.Connection, viejas: list[int],
                           sucesora: int) -> None:
    for vid in viejas:
        conn.execute("UPDATE interactions SET superseded_by = ? WHERE id = ?",
                     (sucesora, vid))


# ─── user_facts: compactación por usuario (T49) ─────────────────────────────

def pinned_user_facts(conn: sqlite3.Connection, handle: str,
                      limit: int = 50) -> list[tuple[int, str]]:
    """Los 📌 de una persona: entran a la respuesta SIEMPRE, sin pasar por la
    búsqueda. Son los que pidió recordar textualmente."""
    return [(r["id"], r["fact_text"]) for r in conn.execute(
        "SELECT id, fact_text FROM user_facts "
        "WHERE handle = ? AND pinned = 1 AND superseded_by IS NULL "
        "ORDER BY id LIMIT ?", (handle, limit)).fetchall()]


def delete_user_fact(conn: sqlite3.Connection, fact_id: int, handle: str) -> str | None:
    """Borra UN hecho de `handle` (y su embedding). Devuelve el texto borrado, o None.

    Scopeado por handle a propósito: aunque el id venga equivocado, nunca puede
    borrar el hecho de otra persona. El índice FTS lo limpia su trigger; el vector
    en vec0 NO tiene trigger y hay que borrarlo a mano (si no, queda huérfano y
    sigue ganando búsquedas con un rowid que ya no existe).
    """
    row = conn.execute(
        "SELECT fact_text FROM user_facts WHERE id = ? AND handle = ?",
        (fact_id, handle)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM user_facts_vec WHERE rowid = ?", (fact_id,))
    conn.execute("DELETE FROM user_facts WHERE id = ?", (fact_id,))
    conn.commit()
    return row["fact_text"]


def set_user_fact_pinned(conn: sqlite3.Connection, fact_id: int, pinned: bool) -> bool:
    cur = conn.execute("UPDATE user_facts SET pinned = ? WHERE id = ?",
                       (1 if pinned else 0, fact_id))
    conn.commit()
    return cur.rowcount > 0


def facts_compactables(conn: sqlite3.Connection, *,
                       min_por_usuario: int = 5) -> list[dict]:
    """Usuarios cuya memoria semántica vale la pena revisar, más cargados primero.

    A diferencia de `interacciones_compactables` acá el grupo es el USUARIO
    entero, no el día: los duplicados de un hecho no salen de una charla larga
    sino de contarlo dos veces con meses de diferencia ("no quiere memes del
    mundial, le ponen muy triste" / "…porque le entristecen"). Agrupar por día
    no los pondría nunca en la misma llamada.

    La cola larga de gente con dos o tres hechos se deja quieta: no hay nada que
    fusionar y cada grupo cuesta una llamada al modelo.

    Los 📌 cuentan como cualquier otro: se pueden fusionar entre sí o con los
    demás (nunca descartar), así que son material. Excluirlos del mínimo dejaba
    afuera justo los casos peores — alguien con tres filas diciendo lo mismo,
    dos de ellas fijadas, no llegaba nunca al umbral.
    """
    filas = conn.execute(
        "SELECT id, handle, fact_text, created_at, pinned FROM user_facts "
        "WHERE superseded_by IS NULL ORDER BY handle, id"
    ).fetchall()
    por_handle: dict[str, list[dict]] = {}
    for r in filas:
        por_handle.setdefault(r["handle"], []).append(
            {"id": r["id"], "text": r["fact_text"], "created_at": r["created_at"],
             "pinned": bool(r["pinned"])})
    return sorted(
        ({"handle": h, "filas": fs} for h, fs in por_handle.items()
         if len(fs) >= min_por_usuario),
        key=lambda g: (-len(g["filas"]), g["handle"]))


def insert_user_fact(conn: sqlite3.Connection, handle: str, fact_text: str,
                     source_uri: str | None = None, *, pinned: bool = False) -> int:
    """Inserta un hecho SIN dedup semántico y devuelve su id.

    `upsert_user_fact` no sirve para la compactación: el texto fusionado se
    parece por definición a los que reemplaza, así que el dedup lo saltearía y
    el pase archivaría los originales sin dejar sucesora. Tampoco commitea, para
    poder correr dentro de la transacción todo-o-nada del pase.
    """
    cur = conn.execute(
        "INSERT INTO user_facts(handle, fact_text, source_uri, pinned) VALUES (?, ?, ?, ?)",
        (handle, fact_text, source_uri, 1 if pinned else 0))
    fid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO user_facts_vec(rowid, embedding, partition_key) VALUES (?, ?, ?)",
        (fid, embed(fact_text), handle))
    return fid


def supersede_user_facts(conn: sqlite3.Connection, viejas: list[int],
                         sucesora: int | None) -> None:
    """Archiva hechos: marca `superseded_by` y les saca el embedding.

    Borrar el vector no es opcional. vec0 no tiene FK cascade ni triggers, así
    que un hecho archivado seguiría siendo el vecino más cercano de sí mismo y
    el dedup de `upsert_user_fact` (k=1) rechazaría el hecho nuevo como
    duplicado de uno que ya nadie lee. `restore` lo re-embebe.
    """
    for vid in viejas:
        conn.execute("UPDATE user_facts SET superseded_by = ? WHERE id = ?",
                     (sucesora if sucesora is not None else vid, vid))
        conn.execute("DELETE FROM user_facts_vec WHERE rowid = ?", (vid,))


def purge_user_memory(
    conn: sqlite3.Connection,
    handle: str,
    *,
    include_relationships: bool = False,
    drop_profile: bool = False,
) -> dict[str, int]:
    """Borra la memoria de UN handle: `user_facts` (+ sus embeddings en `user_facts_vec`)
    y sus `events` propios. Nunca toca datos de otros handles.

    Base de `resetme` (T11: solo lo anterior) y `blockme` (T10: además
    `include_relationships` + `drop_profile`). Los embeddings vec0 no los cubre el
    FK cascade → se borran a mano por rowid antes de los facts. Devuelve conteos.
    """
    fact_ids = [r[0] for r in conn.execute(
        "SELECT id FROM user_facts WHERE handle = ?", (handle,)).fetchall()]
    for fid in fact_ids:
        conn.execute("DELETE FROM user_facts_vec WHERE rowid = ?", (fid,))
    conn.execute("DELETE FROM user_facts WHERE handle = ?", (handle,))  # trigger limpia el FTS
    events = conn.execute("DELETE FROM events WHERE handle = ?", (handle,)).rowcount
    inters = conn.execute("DELETE FROM interactions WHERE handle = ?", (handle,)).rowcount
    rels = 0
    if include_relationships:
        rels = conn.execute(
            "DELETE FROM relationships WHERE handle_a = ? OR handle_b = ?", (handle, handle)
        ).rowcount
    if drop_profile:
        # bot_posts.reply_to_handle → users(handle) es NO ACTION: hay que soltar la
        # referencia antes de borrar la fila (si no, falla el FK). Se preserva el log
        # de salida del bot, solo se desvincula del handle.
        conn.execute("UPDATE bot_posts SET reply_to_handle = NULL WHERE reply_to_handle = ?", (handle,))
        conn.execute("DELETE FROM users WHERE handle = ?", (handle,))
    conn.commit()
    return {"facts": len(fact_ids), "events": events, "relationships": rels,
            "interactions": inters}


def migrate_user_handle(conn: sqlite3.Connection, old_handle: str, new_handle: str) -> dict[str, int]:
    """El usuario se cambió el handle en Bluesky (mismo DID): mueve TODA su memoria
    de `old_handle` a `new_handle` y borra la fila vieja de `users`.

    Precondición: la fila de `new_handle` ya existe en `users` (LoadContextNode la
    crea al ver la mención). Los vectores de `user_facts_vec` están particionados
    por handle y vec0 no updatea `partition_key` in place → se releen los embeddings
    y se reinsertan con la partición nueva (sin re-embeber). Devuelve conteos.
    """
    fact_ids = [r[0] for r in conn.execute(
        "SELECT id FROM user_facts WHERE handle = ?", (old_handle,)).fetchall()]
    for fid in fact_ids:
        row = conn.execute(
            "SELECT embedding FROM user_facts_vec WHERE rowid = ?", (fid,)).fetchone()
        conn.execute("DELETE FROM user_facts_vec WHERE rowid = ?", (fid,))
        if row is not None:
            conn.execute(
                "INSERT INTO user_facts_vec(rowid, embedding, partition_key) VALUES (?, ?, ?)",
                (fid, row[0], new_handle),
            )
    conn.execute("UPDATE user_facts SET handle = ? WHERE handle = ?", (new_handle, old_handle))
    events = conn.execute(
        "UPDATE events SET handle = ? WHERE handle = ?", (new_handle, old_handle)).rowcount
    # relationships tiene PK (a, b, kind): si ya existe la arista con el handle nuevo,
    # la vieja se descarta (OR IGNORE + delete de las sobrantes).
    conn.execute("UPDATE OR IGNORE relationships SET handle_a = ? WHERE handle_a = ?",
                 (new_handle, old_handle))
    conn.execute("UPDATE OR IGNORE relationships SET handle_b = ? WHERE handle_b = ?",
                 (new_handle, old_handle))
    conn.execute("DELETE FROM relationships WHERE handle_a = ? OR handle_b = ?",
                 (old_handle, old_handle))
    conn.execute("UPDATE bot_posts SET reply_to_handle = ? WHERE reply_to_handle = ?",
                 (new_handle, old_handle))
    conn.execute("UPDATE interactions SET handle = ? WHERE handle = ?",
                 (new_handle, old_handle))
    conn.execute("DELETE FROM users WHERE handle = ?", (old_handle,))
    conn.commit()
    return {"facts": len(fact_ids), "events": events}


# ─── posted_news: dedup de items RSS ya posteados (T15) ───────────────────────
def news_item_posted(conn: sqlite3.Connection, item_id: str) -> bool:
    """True si el item RSS (por link/guid) ya fue posteado."""
    return conn.execute(
        "SELECT 1 FROM posted_news WHERE item_id = ?", (item_id,)
    ).fetchone() is not None


def mark_news_item_posted(conn: sqlite3.Connection, item_id: str,
                          source: str | None = None, title: str | None = None) -> None:
    """Marca un item RSS como posteado (idempotente)."""
    conn.execute(
        "INSERT OR IGNORE INTO posted_news (item_id, source, title) VALUES (?, ?, ?)",
        (item_id, source, title),
    )
    conn.commit()
