"""router.py — router de modelos con fallbacks, endpoint-agnóstico.

Tres capas desacopladas (config en settings.json → sección MODELS):
  1. endpoints: cualquier API OpenAI-compatible (OpenRouter, Ollama local, etc.)
  2. aliases:   nombre lógico → CADENA DE FALLBACK ordenada de {endpoint, model}
  3. roles:     función del bot (classify|reply|...) → alias

El código pide un *rol* (`router.complete("reply", ...)`), se resuelve al alias y se
recorre su cadena probando cada endpoint con reintentos+backoff; si uno falla, pasa al
siguiente. Así un modelo local puede ser fallback de uno remoto (o al revés) sin tocar
el código, solo la config.

Back-compat: si no hay sección MODELS, `build_router` deriva los aliases de los
REASONING_MODEL / LITE_MODEL / IMAGE_MODEL previos sobre un único endpoint.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from openai import OpenAI
from pydantic import BaseModel

log = logging.getLogger("botata.router")

# Roles por defecto (usados en el modo back-compat). Mapean función → alias.
_DEFAULT_ROLES: dict[str, str] = {
    "classify":       "lite",
    "reply":          "reasoning",
    "admin":          "reasoning",
    "update_profile": "reasoning",
    "feed_summary":   "lite",
    "feed_opinion":   "lite",
    "bio_interp":     "lite",
    "image_describe": "vision",
}


@dataclass
class _Target:
    endpoint: str
    model: str
    client: OpenAI


class ModelRouter:
    """Rutea llamadas LLM por rol, con fallback entre endpoints/modelos."""

    def __init__(
        self,
        endpoints: dict[str, dict],       # {name: {base_url, api_key}}
        aliases: dict[str, list[dict]],   # {alias: [{endpoint, model}, ...]}
        roles: dict[str, str],            # {role: alias}
        *,
        max_retries: int = 2,
        backoff_base: float = 1.0,
    ) -> None:
        # timeout explícito y max_retries=0 en el SDK: sin esto, el default del
        # SDK de OpenAI es 600s por request CON 2 reintentos internos propios —
        # una request colgada = ~30 min de silencio total ANTES de que el router
        # se entere y pueda reintentar/fallbackear. Los reintentos viven en UNA
        # sola capa: este router (backoff + cadena de fallback).
        self._clients: dict[str, OpenAI] = {
            name: OpenAI(
                api_key=cfg.get("api_key") or "x",
                base_url=cfg["base_url"],
                timeout=float(cfg.get("timeout_s", 120)),
                max_retries=0,
            )
            for name, cfg in endpoints.items()
        }
        self._aliases = aliases
        self._roles = roles
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    # ── resolución ───────────────────────────────────────────────────────────
    def _chain(self, role: str) -> list[_Target]:
        """Cadena de fallback (targets válidos) para un rol."""
        alias = self._roles.get(role)
        if alias is None:
            raise KeyError(f"rol sin mapear: {role!r}")
        targets = self._aliases.get(alias)
        if not targets:
            raise KeyError(f"alias {alias!r} (rol {role!r}) sin targets")
        out: list[_Target] = []
        for t in targets:
            client = self._clients.get(t["endpoint"])
            if client is None:
                log.warning("endpoint %r desconocido en alias %r — omitido", t["endpoint"], alias)
                continue
            out.append(_Target(t["endpoint"], t["model"], client))
        if not out:
            raise KeyError(f"alias {alias!r} sin endpoints válidos")
        return out

    def _run(self, role: str, fn: Callable[[OpenAI, str], Any]) -> Any:
        """Recorre la cadena del rol; `fn(client, model)` hace la llamada real.
        Reintenta cada target con backoff exponencial antes de pasar al siguiente."""
        chain = self._chain(role)
        last_exc: Exception | None = None
        for idx, target in enumerate(chain):
            for attempt in range(self._max_retries):
                try:
                    result = fn(target.client, target.model)
                    if idx > 0 or attempt > 0:
                        log.info("router[%s]: sirvió %s/%s", role, target.endpoint, target.model)
                    return result
                except Exception as e:  # noqa: BLE001 — el router es el borde de resiliencia
                    last_exc = e
                    log.warning(
                        "router[%s]: falló %s/%s (intento %d/%d): %s",
                        role, target.endpoint, target.model, attempt + 1, self._max_retries, e,
                    )
                    if attempt < self._max_retries - 1:
                        time.sleep(self._backoff_base * (2 ** attempt))
        raise RuntimeError(f"router[{role}]: se agotaron los endpoints") from last_exc

    # ── API de alto nivel ────────────────────────────────────────────────────
    def complete(self, role: str, system: str, user: str, response_model: type[BaseModel]) -> BaseModel:
        """Structured output → instancia pydantic (guided_json cuando el endpoint lo soporta)."""
        schema = response_model.model_json_schema()

        def fn(client: OpenAI, model: str) -> BaseModel:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                extra_body={"guided_json": schema},
            )
            raw = resp.choices[0].message.content
            return response_model.model_validate_json(raw)

        return self._run(role, fn)

    def call_with_tools(self, role: str, system: str, user: str, tools: list[dict]) -> tuple[str | None, list]:
        """Tool-calling. Devuelve (texto, tool_calls); exactamente uno no vacío."""
        def fn(client: OpenAI, model: str) -> tuple[str | None, list]:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=tools,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            if msg.tool_calls:
                return None, msg.tool_calls
            return msg.content or "", []

        return self._run(role, fn)

    def chat(self, role: str, messages: list[dict], **kwargs: Any) -> str:
        """Chat plano → texto. Para prompts que no necesitan structured output."""
        def fn(client: OpenAI, model: str) -> str:
            resp = client.chat.completions.create(model=model, messages=messages, **kwargs)
            return resp.choices[0].message.content or ""

        return self._run(role, fn)

    # ── introspección ────────────────────────────────────────────────────────
    def describe(self) -> str:
        """Resumen legible de roles → cadena de fallback (para logs de arranque)."""
        parts = []
        for role, alias in sorted(self._roles.items()):
            chain = " → ".join(
                f"{t['endpoint']}:{t['model']}" for t in self._aliases.get(alias, [])
            )
            parts.append(f"{role}={chain}")
        return " | ".join(parts)


class RoleLLM:
    """Adaptador fino: liga un ModelRouter a un rol fijo, con la misma interfaz que
    consumían los nodos (.complete / .call_with_tools / .chat) — así el rewire de los
    nodos es mínimo."""

    def __init__(self, router: ModelRouter, role: str) -> None:
        self._router = router
        self._role = role

    def complete(self, system: str, user: str, response_model: type[BaseModel]) -> BaseModel:
        return self._router.complete(self._role, system, user, response_model)

    def call_with_tools(self, system: str, user: str, tools: list[dict]) -> tuple[str | None, list]:
        return self._router.call_with_tools(self._role, system, user, tools)

    def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return self._router.chat(self._role, messages, **kwargs)


def build_router(
    models_config: dict | None,
    *,
    legacy: dict,
    env: Mapping[str, str],
    max_retries: int = 2,
) -> ModelRouter:
    """Construye el router desde settings["MODELS"], o en modo back-compat desde
    los modelos sueltos previos (`legacy`).

    endpoints admiten `api_key` literal (ej. 'ollama') o `api_key_env` (nombre de var
    de entorno). `legacy` = {base_url, api_key, reasoning, lite, vision}.
    """
    if models_config:
        endpoints: dict[str, dict] = {}
        for name, cfg in models_config["endpoints"].items():
            api_key = cfg.get("api_key")
            if not api_key and cfg.get("api_key_env"):
                api_key = env.get(cfg["api_key_env"], "")
            endpoints[name] = {"base_url": cfg["base_url"], "api_key": api_key or "x"}
            if "timeout_s" in cfg:
                endpoints[name]["timeout_s"] = cfg["timeout_s"]
        aliases = models_config["aliases"]
        roles = models_config.get("roles", _DEFAULT_ROLES)
    else:
        endpoints = {"default": {"base_url": legacy["base_url"], "api_key": legacy["api_key"]}}
        aliases = {
            "reasoning": [{"endpoint": "default", "model": legacy["reasoning"]}],
            "lite":      [{"endpoint": "default", "model": legacy["lite"]}],
            "vision":    [{"endpoint": "default", "model": legacy["vision"]}],
        }
        roles = dict(_DEFAULT_ROLES)

    return ModelRouter(endpoints, aliases, roles, max_retries=max_retries)
