"""migrate_maripobot.py — migración one-off de la data de maripobot a butterbot.db.

Source: maripobot_deprecated/context/ (la data viva completa).
  - perfiles.json + users/*.md  → users + user_facts
  - history.md "## Lecciones destiladas" → lessons

Carga vía db.upsert_user_fact / db.upsert_lesson (dedup semántico → idempotente).

Formato de perfil (template full de maripobot):
    # Perfil Bluesky: @<handle>
    ## Datos básicos
    | Campo | Detalle |        ← tabla → facts "Campo: valor" (Alias, Profesión, ...)
    |---|---|
    | Handle | @x |
    ## Identidad y lore            ┐
    - <bullet>                      │
    ## Historial en Bluesky         ├ todo bullet "- " → user_fact
    - <bullet>                      │  (salvo "no se revela ningún dato...", que es meta)
    ## Memoria                      │
    - <bullet>                      ┘

Formato de lecciones: bullets "- " bajo "## Lecciones destiladas" (hasta la próxima "## ").
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

import db

log = logging.getLogger("butterbot.migrate")

BASE_DIR = Path(__file__).parent
SRC_DIR = BASE_DIR / "maripobot_deprecated" / "context"
PROFILES_JSON = SRC_DIR / "perfiles.json"
USERS_DIR = SRC_DIR / "users"
HISTORY_MD = SRC_DIR / "history.md"

_NON_FACT = "no se revela ningún dato"


def _parse_profile(md: str) -> tuple[str | None, list[str]]:
    """Devuelve (handle, [fact_text, ...]) desde el markdown de un perfil.

    Extrae como hechos todos los bullets "- " y las filas de tablas "| Campo | valor |"
    (saltando header/separador y la meta-nota 'no se revela ningún dato...').
    """
    handle: str | None = None
    facts: list[str] = []
    for raw in md.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("# ") and handle is None:
            h = re.sub(r"(?i)^Perfil\s+Bluesky\s*:\s*", "", s[2:]).strip().lstrip("@")
            handle = h or None
            continue
        if s.startswith("## "):
            continue
        if s.startswith("- "):
            fact = s[2:].strip()
            if fact and _NON_FACT not in fact.lower():
                facts.append(fact)
            continue
        if s.startswith("|") and "---" not in s:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 2 and cells[0].lower() not in ("campo", "detalle"):
                facts.append(f"{cells[0]}: {cells[1]}")
    return handle, facts


def _parse_lessons(md: str) -> list[str]:
    """Extrae los bullets de la sección '## Lecciones destiladas' del history.md."""
    lessons: list[str] = []
    in_section = False
    for raw in md.splitlines():
        s = raw.strip()
        if s.startswith("## "):
            in_section = s[3:].strip().lower() == "lecciones destiladas"
            continue
        if in_section and s.startswith("- "):
            lesson = s[2:].strip()
            if lesson:
                lessons.append(lesson)
    return lessons


def _upsert_user(conn: sqlite3.Connection, handle: str) -> None:
    conn.execute(
        "INSERT INTO users(handle) VALUES (?) ON CONFLICT(handle) DO NOTHING",
        (handle,),
    )


def migrate(conn: sqlite3.Connection) -> None:
    # ── perfiles → users + user_facts ─────────────────────────────────
    profiles: dict[str, str] = json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    log.info("migrando %d perfil(es) desde %s", len(profiles), PROFILES_JSON)
    total_users = 0
    total_facts = 0
    for handle, filename in profiles.items():
        path = USERS_DIR / filename
        if not path.exists():
            log.warning("perfil listado pero ausente: %s", path)
            continue
        parsed_handle, facts = _parse_profile(path.read_text(encoding="utf-8"))
        h = parsed_handle or handle
        _upsert_user(conn, h)
        total_users += 1
        new_facts = 0
        for fact_text in facts:
            new_id = db.upsert_user_fact(conn, h, fact_text, source_uri="migrated:deprecated")
            if new_id is not None:
                new_facts += 1
                total_facts += 1
        log.info("  @%s: %d hechos nuevos (de %d)", h, new_facts, len(facts))

    # ── history.md → lessons ──────────────────────────────────────────
    total_lessons = 0
    if HISTORY_MD.exists():
        lessons = _parse_lessons(HISTORY_MD.read_text(encoding="utf-8"))
        log.info("migrando %d lección(es) desde %s", len(lessons), HISTORY_MD)
        for lesson_text in lessons:
            new_id = db.upsert_lesson(conn, lesson_text, scope="community")
            if new_id is not None:
                total_lessons += 1
    else:
        log.warning("no existe %s — sin lecciones que migrar", HISTORY_MD)

    conn.commit()
    log.info(
        "migración completa: %d usuario(s), %d hecho(s) nuevos, %d lección(es) nuevas",
        total_users, total_facts, total_lessons,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    conn = db.init_db()  # crea tablas nuevas (IF NOT EXISTS) sobre butterbot.db real
    migrate(conn)
    conn.close()
