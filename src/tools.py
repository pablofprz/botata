"""tools.py — framework de tools de botata (registry + scopes + config).

Reemplaza el `ADMIN_TOOLS` hardcodeado por un registro central. Cada tool se
declara UNA vez (schema OpenAI + handler + scopes) y el grafo arma en runtime la
lista disponible filtrando por:
  - `enabled`: toggle por config (settings.json → sección TOOLS).
  - `scope`:   contexto donde la tool tiene sentido (reply | feed_reflection | admin).

Infra genérica y agnóstica de la app: NO conoce dbmod, paths ni prompts. Los
handlers concretos viven en botata.py y se cierran sobre lo que necesitan; acá
solo se define el contrato (Tool/ToolContext/ToolResult) y el ruteo.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("botata.tools")


# ─── Scopes ──────────────────────────────────────────────────────────────────
class Scope:
    """Contextos donde una tool puede ofrecerse al LLM."""
    REPLY = "reply"                    # respondiendo una mention
    FEED_REFLECTION = "feed_reflection"  # loop proactivo leyendo el feed
    ADMIN = "admin"                   # comandos del admin


ALL_SCOPES: frozenset[str] = frozenset({Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN})


def norm_handle(handle: str | None) -> str:
    """Forma canónica de un handle para comparar membresías: sin @, lowercase."""
    return (handle or "").strip().lstrip("@").lower()


# Prefijo para membresía dinámica en USER_GROUPS: "feed:polcifeed" = todos los
# miembros de ese feed (resueltos por el feed_resolver inyectado en set_groups).
FEED_REF_PREFIX = "feed:"


# ─── Contrato ────────────────────────────────────────────────────────────────
@dataclass
class ToolResult:
    """Lo que devuelve un handler: texto de confirmación + imagen opcional a adjuntar."""
    text: str
    image_path: str | None = None


@dataclass
class ToolContext:
    """Todo lo que un handler necesita del runtime. `state` es el MentionState (o el
    estado del loop proactivo); `conn` es la conexión sqlite. Handlers que necesiten
    más se cierran sobre los globals de botata.py."""
    state: dict[str, Any]
    conn: Any


ToolHandler = Callable[[dict, ToolContext], ToolResult]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]       # JSON schema de los parámetros (formato OpenAI)
    handler: ToolHandler
    scopes: frozenset[str]
    enabled: bool = True
    # Grupos de usuarios que pueden gatillar la tool (nombres de USER_GROUPS).
    # None = sin restricción (cualquier usuario). El admin siempre bypassea.
    groups: frozenset[str] | None = None

    def to_openai(self) -> dict:
        """Schema en el formato que espera la API de tool-calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ─── Registry ────────────────────────────────────────────────────────────────
class ToolRegistry:
    """Registro central de tools. Filtra por scope + enabled y rutea la ejecución."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._group_members: dict[str, frozenset[str]] = {}  # grupo → handles estáticos
        self._group_feeds: dict[str, tuple[str, ...]] = {}   # grupo → refs "feed:<name>"
        self._admins: frozenset[str] = frozenset()           # bypassean todo check de grupo
        # feed_resolver(feed_name) → iterable de handles; lo inyecta el caller
        # (el registry no conoce la plataforma). El caching es responsabilidad
        # del resolver — acá se llama en cada check de membresía.
        self._feed_resolver: Callable[[str], Any] | None = None

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
        scopes: frozenset[str] | set[str] | list[str],
        *,
        enabled: bool = True,
        groups: frozenset[str] | set[str] | list[str] | None = None,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool ya registrada: {name}")
        bad = set(scopes) - ALL_SCOPES
        if bad:
            raise ValueError(f"scope(s) inválido(s) para {name}: {bad}")
        self._tools[name] = Tool(name, description, parameters, handler, frozenset(scopes),
                                 enabled, frozenset(groups) if groups else None)

    def set_groups(self, groups: dict[str, list[str]] | None,
                   admin_handle: str | None = None,
                   feed_resolver: "Callable[[str], Any] | None" = None) -> None:
        """Carga las membresías (settings.json → USER_GROUPS) y el admin (bypass).

        Cada entrada de un grupo es un handle o una ref "feed:<name>" (todos los
        miembros de ese feed, resueltos en vivo por `feed_resolver`). Un grupo
        referenciado por una tool pero ausente acá no matchea a nadie: default
        cerrado — un typo restringe de más, nunca abre de más.
        """
        self._group_members, self._group_feeds = {}, {}
        for g, members in (groups or {}).items():
            feeds = tuple(m[len(FEED_REF_PREFIX):].strip() for m in members
                          if isinstance(m, str) and m.startswith(FEED_REF_PREFIX))
            statics = frozenset(norm_handle(m) for m in members
                                if not (isinstance(m, str) and m.startswith(FEED_REF_PREFIX))
                                and norm_handle(m))
            self._group_members[g] = statics
            self._group_feeds[g] = feeds
        self._admins = frozenset({norm_handle(admin_handle)} if norm_handle(admin_handle) else ())
        self._feed_resolver = feed_resolver

    def _in_group(self, handle: str, group: str) -> bool:
        if handle in self._group_members.get(group, frozenset()):
            return True
        for ref in self._group_feeds.get(group, ()):
            if self._feed_resolver is None:
                continue  # sin resolver (contexto sin plataforma) → cerrado
            try:
                members = self._feed_resolver(ref) or ()
            except Exception as e:  # resolver jamás debe tirar el flujo de reply
                log.warning("grupos: feed_resolver('%s') falló: %s", ref, e)
                continue
            if handle in {norm_handle(m) for m in members}:
                return True
        return False

    def groups_for(self, handle: str | None) -> list[str]:
        """Grupos de USER_GROUPS a los que pertenece `handle` (para /check-role).

        Ordenado y estable. Handle vacío/desconocido → sin grupos.
        """
        h = norm_handle(handle)
        if not h:
            return []
        names = set(self._group_members) | set(self._group_feeds)
        return sorted(g for g in names if self._in_group(h, g))

    def allowed_for(self, tool: Tool, handle: str | None) -> bool:
        """¿Puede `handle` gatillar esta tool? Sin grupos declarados → sí. El admin
        siempre. Con grupos y handle desconocido/ausente → no (cerrado)."""
        if tool.groups is None:
            return True
        h = norm_handle(handle)
        if h and h in self._admins:
            return True
        return bool(h) and any(self._in_group(h, g) for g in tool.groups)

    def apply_config(self, config: dict[str, dict]) -> None:
        """Aplica overrides de settings.json → sección TOOLS.

        `{name: {"enabled": bool, "scopes": [...], "groups": [...]}}`. Una tool
        ausente del config conserva sus defaults declarados. Config para una tool
        inexistente se ignora con warning (evita fallar el arranque por un typo).
        `groups` vacío o null = sin restricción de usuarios.
        """
        for name, cfg in config.items():
            tool = self._tools.get(name)
            if tool is None:
                log.warning("TOOLS config: tool desconocida '%s' — ignorada", name)
                continue
            if "enabled" in cfg:
                tool.enabled = bool(cfg["enabled"])
            if "scopes" in cfg:
                bad = set(cfg["scopes"]) - ALL_SCOPES
                if bad:
                    log.warning("TOOLS config: scope(s) inválido(s) para '%s': %s — ignorados", name, bad)
                tool.scopes = frozenset(set(cfg["scopes"]) & ALL_SCOPES)
            if "groups" in cfg:
                tool.groups = frozenset(cfg["groups"]) if cfg["groups"] else None

    def available(self, scope: str, handle: str | None = None) -> list[Tool]:
        """Tools habilitadas cuyo scope incluye `scope`.

        Con `handle` (flujo reply: hay un usuario pidiendo) se filtran además las
        tools restringidas por grupo a las que ese handle no pertenece. Sin handle
        (contextos sin solicitante: feed_reflection, admin) no hay filtro de grupo.
        """
        out = [t for t in self._tools.values() if t.enabled and scope in t.scopes]
        if handle is not None:
            out = [t for t in out if self.allowed_for(t, handle)]
        return out

    def openai_schemas(self, scope: str, handle: str | None = None) -> list[dict]:
        """Schemas OpenAI de las tools disponibles en `scope` (lista para tool-calling)."""
        return [t.to_openai() for t in self.available(scope, handle)]

    def execute(self, name: str, args: dict, ctx: ToolContext,
                handle: str | None = None) -> ToolResult:
        """Ejecuta una tool por nombre. Falla graceful si no existe o está deshabilitada.
        Con `handle`, aplica además el check de grupos (defensa en profundidad: aunque
        el LLM alucine una tool que no le fue ofrecida, acá se corta)."""
        tool = self._tools.get(name)
        if tool is None:
            log.warning("execute: tool desconocida '%s'", name)
            return ToolResult(text=f"[tool desconocida: {name}]")
        if not tool.enabled:
            log.warning("execute: tool deshabilitada '%s'", name)
            return ToolResult(text=f"[tool deshabilitada: {name}]")
        if handle is not None and not self.allowed_for(tool, handle):
            log.warning("execute: @%s sin permiso de grupo para '%s'", handle, name)
            return ToolResult(text=f"[no tenés permiso para usar {name}]")
        return tool.handler(args, ctx)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)
