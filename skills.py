"""skills.py — workspace de archivos de comportamiento (T26).

Capacidades del bot definidas en markdown (`skills/*.md`), editables sin tocar
código ni redeploy. Separación conceptual: SOUL.md = comportamiento permanente;
skills = conocimiento/procedimientos TEMÁTICOS que se cargan cuando aplican.

Selección (inyección selectiva, no todo siempre):
- El system prompt lleva un ÍNDICE liviano (nombre + descripción por skill).
- El cuerpo se carga on-demand con la tool `use_skill(name)` (fase de tools T24).
- `inline: true` en el frontmatter fuerza el cuerpo completo al prompt (para
  skills que el agente no sabría pedir). El contexto admin inyecta todo inline
  (su flujo de tools no permite encadenar use_skill + respuesta).

Formato (`skills/*.md`, frontmatter simple entre líneas `---`):
    ---
    name: reglas-comunidad
    description: Cómo responder preguntas sobre las reglas de la comunidad
    scopes: reply, admin        # default: reply, feed_reflection, admin
    enabled: true               # default true
    inline: false               # default false (on-demand vía use_skill)
    ---
    (instrucciones que ve el LLM al cargar la skill)

El frontmatter ES la config (no hay sección SKILLS en settings.json): archivo
autodescriptivo que el admin/moderador edita entero. Los archivos se releen en
cada pase (mismo patrón sin cache que SOUL/MEMORY) → edición en caliente.

Infra genérica estilo tools.py: no conoce butterbot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tools import ALL_SCOPES

log = logging.getLogger("butterbot.skills")

_DEFAULT_SCOPES: frozenset[str] = frozenset(ALL_SCOPES)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    scopes: frozenset[str] = field(default_factory=lambda: _DEFAULT_SCOPES)
    enabled: bool = True
    inline: bool = False


# ─── Parser de frontmatter (stdlib, formato key: value) ─────────────────────
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


def _parse_skill(path: Path) -> Skill | None:
    parsed = _parse_frontmatter(path.read_text(encoding="utf-8"))
    if parsed is None:
        log.warning("skill '%s': sin frontmatter — ignorada", path.name)
        return None
    meta, body = parsed
    name, description = meta.get("name"), meta.get("description")
    if not name or not description:
        log.warning("skill '%s': falta name o description — ignorada", path.name)
        return None
    raw_scopes = {s.strip() for s in meta.get("scopes", "").split(",") if s.strip()}
    bad = raw_scopes - ALL_SCOPES
    if bad:
        log.warning("skill '%s': scope(s) inválido(s) %s — ignorados", name, bad)
    scopes = frozenset(raw_scopes & ALL_SCOPES) or _DEFAULT_SCOPES
    return Skill(
        name=name, description=description, body=body, path=path, scopes=scopes,
        enabled=meta.get("enabled", "true").lower() != "false",
        inline=meta.get("inline", "false").lower() == "true",
    )


# ─── API ─────────────────────────────────────────────────────────────────────
def load_skills(skills_dir: Path) -> list[Skill]:
    """Carga las skills habilitadas. Un archivo roto no frena a los demás."""
    if not skills_dir.is_dir():
        return []
    out = []
    for path in sorted(skills_dir.glob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        try:
            skill = _parse_skill(path)
        except Exception as e:
            log.warning("skill '%s': error al parsear (%s) — ignorada", path.name, e)
            continue
        if skill and skill.enabled:
            out.append(skill)
    return out


def skills_prompt_block(skills_dir: Path, scope: str, *, all_inline: bool = False) -> str:
    """Sección de skills para el system prompt de `scope`. "" si no hay ninguna.

    Modo normal: cuerpos de las `inline:true` + índice de las on-demand (el
    agente las pide con use_skill). `all_inline=True` (contexto admin) inyecta
    todos los cuerpos.
    """
    relevant = [s for s in load_skills(skills_dir) if scope in s.scopes]
    if not relevant:
        return ""
    inlined = [s for s in relevant if s.inline or all_inline]
    on_demand = [s for s in relevant if not (s.inline or all_inline)]
    parts = []
    for s in inlined:
        parts.append(f"## Skill: {s.name} ({s.description})\n{s.body}")
    if on_demand:
        index = "\n".join(f"- {s.name}: {s.description}" for s in on_demand)
        parts.append(
            "Skills disponibles — si el tema de la conversación coincide con alguna, "
            "llamá la tool use_skill(name) para cargar sus instrucciones:\n" + index
        )
    return "Skills (guías de comportamiento por tema):\n" + "\n\n".join(parts)


def get_skill_body(skills_dir: Path, name: str) -> str | None:
    """Cuerpo de una skill habilitada por nombre (para la tool use_skill)."""
    for s in load_skills(skills_dir):
        if s.name == name:
            return f"## Skill: {s.name}\n{s.body}"
    return None
