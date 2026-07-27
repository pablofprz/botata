"""moods.py — estados de ánimo del bot (registro conductual variable por día).

Un "mood" tiñe CÓMO responde el bot durante un día: un día más pila, otro más
bajón, otro filoso. A diferencia de SOUL.md (comportamiento permanente) y de las
skills (conocimiento temático que se carga cuando aplica), el mood es un registro
afectivo transversal que rota — manual (el admin lo fija o lo agenda) o auto (el
bot lo elige leyendo el clima de la comunidad + su propia actividad).

Formato (`moods/*.md`, frontmatter simple entre líneas `---`, igual que skills):
    ---
    name: bajon
    description: Melancólico, parco, responde corto y sin ganas de joda
    triggers: me ignoraron varios días, perdió mi equipo, fechas tristes   # opcional
    enabled: true               # default true
    ---
    (instrucciones de tono que ve el LLM cuando este mood está activo)

`triggers` (opcional, prompt libre) dice QUÉ pone al bot en ese estado: el
selector automático de mood lo lee junto al clima de la comunidad para elegir
con causa ("me bardearon mucho" → angry). Sin triggers, el mood se elige solo
por su description.

El frontmatter ES la config del mood (no hay lista de moods en settings.json — ahí
solo vive el toggle/modo/schedule). Los archivos se releen en cada pase (edición en
caliente, mismo patrón que skills/SOUL). Infra genérica: no conoce botata.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("botata.moods")


@dataclass
class Mood:
    name: str
    description: str
    body: str
    triggers: str = ""   # qué lo dispara (prompt libre; "" = sin triggers declarados)
    enabled: bool = True


# ─── Parser de frontmatter (stdlib, key: value) ─────────────────────────────
def _parse_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Separa frontmatter y cuerpo. None si el archivo no tiene frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[i + 1:]).strip()
            return meta, body
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.split("#")[0].strip()
    return None  # frontmatter sin cerrar


def _parse_mood(path: Path) -> Mood | None:
    parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
    if parsed is None:
        log.warning("mood '%s': sin frontmatter — ignorado", path.name)
        return None
    meta, body = parsed
    name, description = meta.get("name"), meta.get("description")
    if not name or not description:
        log.warning("mood '%s': falta name o description — ignorado", path.name)
        return None
    return Mood(
        name=name, description=description, body=body,
        triggers=meta.get("triggers", "").strip(),
        enabled=meta.get("enabled", "true").lower() != "false",
    )


# ─── API ─────────────────────────────────────────────────────────────────────
def load_moods(moods_dir: Path) -> dict[str, Mood]:
    """Moods habilitados por nombre. Un archivo roto no frena a los demás."""
    out: dict[str, Mood] = {}
    if not moods_dir.is_dir():
        return out
    for path in sorted(moods_dir.glob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        try:
            mood = _parse_mood(path)
        except Exception as e:
            log.warning("mood '%s': error al parsear (%s) — ignorado", path.name, e)
            continue
        if mood and mood.enabled:
            out[mood.name] = mood
    return out


def get_mood(moods_dir: Path, name: str) -> Mood | None:
    """Un mood habilitado por nombre (None si no existe o está deshabilitado)."""
    if not name:
        return None
    return load_moods(moods_dir).get(name.strip())


def mood_index(moods_dir: Path) -> list[tuple[str, str, str]]:
    """(name, description, triggers) de los moods habilitados — para el prompt
    de auto-selección ("" en triggers si el mood no los declara)."""
    return [(m.name, m.description, m.triggers) for m in load_moods(moods_dir).values()]


def mood_prompt_block(mood: Mood) -> str:
    """Sección para el system prompt: el bot está HOY con este humor."""
    return (
        f"Tu estado de ánimo HOY es «{mood.name}» ({mood.description}). "
        f"Que se note en el tono, sin romper tu personalidad de base:\n{mood.body}"
    )
