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

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
        scopes: frozenset[str] | set[str] | list[str],
        *,
        enabled: bool = True,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool ya registrada: {name}")
        bad = set(scopes) - ALL_SCOPES
        if bad:
            raise ValueError(f"scope(s) inválido(s) para {name}: {bad}")
        self._tools[name] = Tool(name, description, parameters, handler, frozenset(scopes), enabled)

    def apply_config(self, config: dict[str, dict]) -> None:
        """Aplica overrides de settings.json → sección TOOLS.

        `{name: {"enabled": bool, "scopes": [...]}}`. Una tool ausente del config
        conserva sus defaults declarados. Config para una tool inexistente se ignora
        con warning (evita fallar el arranque por un typo).
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

    def available(self, scope: str) -> list[Tool]:
        """Tools habilitadas cuyo scope incluye `scope`."""
        return [t for t in self._tools.values() if t.enabled and scope in t.scopes]

    def openai_schemas(self, scope: str) -> list[dict]:
        """Schemas OpenAI de las tools disponibles en `scope` (lista para tool-calling)."""
        return [t.to_openai() for t in self.available(scope)]

    def execute(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        """Ejecuta una tool por nombre. Falla graceful si no existe o está deshabilitada."""
        tool = self._tools.get(name)
        if tool is None:
            log.warning("execute: tool desconocida '%s'", name)
            return ToolResult(text=f"[tool desconocida: {name}]")
        if not tool.enabled:
            log.warning("execute: tool deshabilitada '%s'", name)
            return ToolResult(text=f"[tool deshabilitada: {name}]")
        return tool.handler(args, ctx)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)
