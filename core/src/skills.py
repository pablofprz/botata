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

Infra genérica estilo tools.py: no conoce botata.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tools import ALL_SCOPES

log = logging.getLogger("botata.skills")

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
    # Palabras que cargan la skill SIN preguntarle al modelo (ver `disparadas`).
    triggers: tuple[str, ...] = ()


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
        triggers=tuple(t.strip().lower() for t in meta.get("triggers", "").split(",")
                       if t.strip()),
    )


# ─── Disparadores: sacar al modelo del medio ────────────────────────────────
# El camino on-demand obliga al LLM a traducir "tema" → nombre exacto de skill,
# y ahí se equivoca: medido en producción, pidió `use_skill("bostera")` cuando
# la skill se llama `boca`, y `use_skill("dinosaurios")`, que no existe. El
# matcheo semántico no salva el caso —"bostera"→boca da 0.385 y
# "dinosaurios"→mapache 0.369, así que cualquier umbral que acierte uno erra el
# otro—, así que el arreglo es no preguntarle: si el admin declara las palabras,
# la skill entra sola.
_SEPARADORES = ".,;:!?¡¿()[]{}\"'…\n\t/\\-—"


def _normalizar(texto: str) -> str:
    bajo = texto.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u")):
        bajo = bajo.replace(a, b)
    for s in _SEPARADORES:
        bajo = bajo.replace(s, " ")
    return f" {' '.join(bajo.split())} "


def disparadas(skills: list[Skill], texto: str) -> list[Skill]:
    """Las skills cuyos `triggers` aparecen en `texto`.

    Match por palabra entera y sin acentos: "boke" dispara con "decime boke" y
    con "BOKE!", pero no con "bokeron". Una palabra suelta como trigger es
    barata de declarar y no depende de que el modelo adivine nada.
    """
    if not texto:
        return []
    plano = _normalizar(texto)
    return [s for s in skills
            if any(f" {_normalizar(t).strip()} " in plano for t in s.triggers)]


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


def skills_prompt_block(skills_dir: Path, scope: str, *, all_inline: bool = False,
                        texto: str = "") -> str:
    """Sección de skills para el system prompt de `scope`. "" si no hay ninguna.

    Modo normal: cuerpos de las `inline:true` + índice de las on-demand (el
    agente las pide con use_skill). `all_inline=True` (contexto admin) inyecta
    todos los cuerpos.
    """
    relevant = [s for s in load_skills(skills_dir) if scope in s.scopes]
    if not relevant:
        return ""
    por_trigger = {s.name for s in disparadas(relevant, texto)}
    if por_trigger:
        log.info("skills disparadas por palabra clave: %s", ", ".join(sorted(por_trigger)))
    inlined = [s for s in relevant if s.inline or all_inline or s.name in por_trigger]
    on_demand = [s for s in relevant if s not in inlined]
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


def resolver_skill(skills: list[Skill], name: str) -> Skill | None:
    """La skill que el modelo quiso pedir, tolerando la variación de nombre.

    Exacto → sin mayúsculas/acentos → tipeo parecido (difflib). NO se intenta
    adivinar por significado: medido, "dinosaurios" queda tan cerca de `mapache`
    como "bostera" de `boca`, así que un umbral semántico cargaría la skill
    equivocada — peor que no cargar ninguna.
    """
    pedido = (name or "").strip()
    if not pedido:
        return None
    for s in skills:
        if s.name == pedido:
            return s
    plano = _normalizar(pedido).strip()
    for s in skills:
        if _normalizar(s.name).strip() == plano:
            return s
    import difflib
    cerca = difflib.get_close_matches(plano, [_normalizar(s.name).strip() for s in skills],
                                      n=1, cutoff=0.8)
    if cerca:
        return next(s for s in skills if _normalizar(s.name).strip() == cerca[0])
    return None


def get_skill_body(skills_dir: Path, name: str, scope: str | None = None) -> str:
    """Cuerpo de una skill para la tool `use_skill`.

    Si el nombre no existe devuelve el ÍNDICE de las que sí, no un error seco.
    La fase de tools es de una sola ronda: con "[skill desconocida]" el modelo
    se queda sin nada y improvisa —en producción contestó "aunque la skill no
    ande"—, mientras que con la lista al menos sabe qué tiene y puede decir algo
    coherente. Y el admin lo ve en el log.
    """
    skills = [s for s in load_skills(skills_dir) if scope is None or scope in s.scopes]
    s = resolver_skill(skills, name)
    if s is not None:
        if s.name != (name or "").strip():
            log.info("use_skill: pidió %r, cargué %r", name, s.name)
        return f"## Skill: {s.name}\n{s.body}"
    log.warning("use_skill: no existe %r — devuelvo el índice (%d skills)", name, len(skills))
    if not skills:
        return "[no hay ninguna skill disponible]"
    indice = "\n".join(f"- {x.name}: {x.description}" for x in skills)
    return (f"[no existe ninguna skill llamada '{name}'. Las que tenés son:\n{indice}\n"
            "Si alguna aplica, usá su nombre EXACTO; si no aplica ninguna, contestá "
            "normalmente y no menciones nada de esto.]")
