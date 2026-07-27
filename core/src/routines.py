"""routines.py — conducta proactiva del bot en archivos (rutinas).

UNA sola idea para todo lo proactivo con cadencia: una rutina =
`routines/<archivo>.md` de la instancia. El código pone los rieles (cadencia
determinística por cursor + destino del post — la lección de T4d: el timing
jamás se delega al LLM); el cuerpo del archivo define TODO el comportamiento.
El ex-heartbeat es simplemente una rutina sin `channel`; los ex-rooms de
Discord son rutinas con `channel`.

    ---
    interval_hours: 4              # cadencia (0 = sin pase proactivo)
    channel: 1488197437813166093   # opcional: canal destino (Discord).
    enabled: true                  # default true
    ---
    Posteá un meme del catálogo, no repitas.
    Actitud acá: shitposting sin filtro.

Sin `channel` la rutina postea al feed principal del canal de la instancia
(Bluesky/Mastodon/canal primario de Discord). Con `channel` (Discord):
- El pase proactivo postea EN ese canal, con su contexto y su dedup propios.
- Si una mención llega desde ese canal (uri "channel_id/message_id"), el
  cuerpo entra al system prompt como registro del lugar. El SOUL y el mood
  siguen SIEMPRE arriba: la rutina MATIZA la identidad, nunca la reemplaza
  (misma relación que los moods con el SOUL).
- `interval_hours: 0` = rutina "solo actitud" (matiza replies, no postea).

El frontmatter ES la config (patrón skills, sin sección en settings.json);
los archivos se releen en cada pase → edición en caliente. Son archivos de la
instancia → solo el admin los escribe (misma regla de seguridad que skills y
moods). Infra genérica: no conoce botata.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from skills import _parse_frontmatter

log = logging.getLogger("botata.routines")


@dataclass
class Routine:
    name: str            # nombre del archivo sin .md (identifica el cursor routine:{name})
    body: str            # instrucciones + actitud (lo que ve el LLM)
    path: Path
    channel: str = ""    # id del canal destino (Discord); "" = feed principal
    interval_hours: float = 0.0   # 0 = sin pase proactivo (solo-actitud si tiene channel)
    enabled: bool = True


def _parse_routine(path: Path) -> Routine | None:
    parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
    if parsed is None:
        log.warning("rutina '%s': sin frontmatter — ignorada", path.name)
        return None
    meta, body = parsed
    if not body.strip():
        log.warning("rutina '%s': sin cuerpo — ignorada", path.name)
        return None
    try:
        interval = float(meta.get("interval_hours", "0") or 0)
    except ValueError:
        log.warning("rutina '%s': interval_hours inválido %r — uso 0 (sin pase)",
                    path.name, meta.get("interval_hours"))
        interval = 0.0
    return Routine(
        name=path.stem, body=body, path=path,
        channel=(meta.get("channel") or "").strip(),
        interval_hours=interval,
        enabled=meta.get("enabled", "true").lower() != "false",
    )


def load_routines(routines_dir: Path, *, include_disabled: bool = False) -> list[Routine]:
    """Rutinas habilitadas de la instancia (con include_disabled, TODAS — para
    contexto de admin: "prendé la de memes" necesita ver las apagadas). Un
    archivo roto no frena a las demás."""
    if not routines_dir.is_dir():
        return []
    out = []
    for path in sorted(routines_dir.glob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        try:
            routine = _parse_routine(path)
        except Exception as e:
            log.warning("rutina '%s': error al parsear (%s) — ignorada", path.name, e)
            continue
        if routine and (routine.enabled or include_disabled):
            out.append(routine)
    return out


def routine_for_uri(routines: list[Routine], uri: str) -> Routine | None:
    """Rutina cuyo canal originó este uri ("channel_id/message_id" en Discord).
    Solo matchean rutinas CON channel; los uris de otros canales (at://...,
    ids de Mastodon) no matchean nunca."""
    for routine in routines:
        if routine.channel and uri.startswith(f"{routine.channel}/"):
            return routine
    return None
