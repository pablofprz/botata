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
import base64
import urllib.request
from dataclasses import dataclass, field
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
    # Generar imágenes: si la instancia no declara el alias, la tool se apaga
    # sola (ver `tiene_rol`) en vez de pedirle un PNG a un modelo de texto.
    "image_generate": "image_gen",
    # Compactar la memoria GENERAL exige razonar sobre contradicciones y sobre
    # qué se puede perder sin dañar al bot: es el trabajo menos apto para un
    # modelo chico. Resumir las notas de un día de charla, en cambio, es
    # resumir — y son muchas llamadas, así que va con el modelo liviano.
    # Compactar los hechos de una persona también es razonar (cuál venció a
    # cuál, qué detalle no se puede perder al fusionar), pero es una llamada por
    # usuario: el tope bajo de `max_usuarios` es lo que lo hace pagable.
    "memory_compact":       "reasoning",
    "facts_compact":        "reasoning",
    "interactions_compact": "lite",
}


def _bytes_de_payload(payload: str | None) -> bytes:
    """Un resultado de imagen viene como data URL, base64 pelado o http(s). Bytes."""
    if not payload:
        raise RuntimeError("el modelo no devolvió ninguna imagen")
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[-1]
    elif payload.startswith(("http://", "https://")):
        # URL efímera del proveedor: se baja acá porque caduca en minutos.
        with urllib.request.urlopen(payload, timeout=60) as resp:
            return resp.read(20_000_000)
    return base64.b64decode(payload)


def _imagen_por_chat(t: _Target, prompt: str) -> str | None:
    """chat/completions pidiendo salida de imagen (la forma de OpenRouter)."""
    resp = t.client.chat.completions.create(
        model=t.model,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"modalities": ["image", "text"]},
    )
    msg = resp.choices[0].message
    # `images` no está en el schema del SDK de OpenAI: viaja como campo extra.
    imgs = getattr(msg, "images", None) or (getattr(msg, "model_extra", None) or {}).get("images")
    for img in imgs or []:
        d = img if isinstance(img, dict) else getattr(img, "model_dump", lambda: {})()
        url = ((d.get("image_url") or {}) or {}).get("url") or d.get("url") or d.get("b64_json")
        if url:
            return url
    return None


def _imagen_por_endpoint_images(t: _Target, prompt: str, size: str | None) -> str | None:
    """`/v1/images/generations` (OpenAI, xAI/Grok, servidores locales)."""
    kw: dict[str, Any] = {"model": t.model, "prompt": prompt, "n": 1}
    if size:
        kw["size"] = size
    try:
        resp = t.client.images.generate(**kw, response_format="b64_json")
    except Exception as e:
        # gpt-image-1 rechaza response_format (siempre devuelve b64); xAI lo exige
        # para no darte una URL que caduca. Se pide, y si molesta se pide sin él.
        if "response_format" not in str(e):
            raise
        log.info("router: %s no acepta response_format, reintento sin él", t.model)
        resp = t.client.images.generate(**kw)
    d = resp.data[0]
    return getattr(d, "b64_json", None) or getattr(d, "url", None)


@dataclass
class _Target:
    endpoint: str
    model: str
    client: OpenAI
    # Resto del hop tal cual está en la config (ej. `api: "images"`). Lo consume
    # quien sabe qué significa; el router solo lo transporta.
    opciones: dict = field(default_factory=dict)


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
            # Un rol nuevo del motor no puede romper las instancias que ya
            # existen: sus `MODELS.roles` fueron escritos antes de que ese rol
            # existiera y nadie va a editar el settings de cada instancia cada
            # vez que se agrega una capacidad. Se cae al mapeo por defecto, y de
            # ahí al primer alias declarado.
            alias = _DEFAULT_ROLES.get(role) or next(iter(self._aliases), None)
            if alias is None:
                raise KeyError(f"rol sin mapear: {role!r}")
            log.warning("rol %r no está en MODELS.roles — uso el alias %r", role, alias)
        targets = self._aliases.get(alias)
        if not targets:
            raise KeyError(f"alias {alias!r} (rol {role!r}) sin targets")
        out: list[_Target] = []
        for t in targets:
            client = self._clients.get(t["endpoint"])
            if client is None:
                log.warning("endpoint %r desconocido en alias %r — omitido", t["endpoint"], alias)
                continue
            out.append(_Target(t["endpoint"], t["model"], client,
                               {k: v for k, v in t.items() if k not in ("endpoint", "model")}))
        if not out:
            raise KeyError(f"alias {alias!r} sin endpoints válidos")
        return out

    def _run(self, role: str, fn: Callable[[_Target], Any]) -> Any:
        """Recorre la cadena del rol; `fn(target)` hace la llamada real.
        Reintenta cada target con backoff exponencial antes de pasar al siguiente."""
        chain = self._chain(role)
        last_exc: Exception | None = None
        for idx, target in enumerate(chain):
            for attempt in range(self._max_retries):
                try:
                    result = fn(target)
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

        def fn(t: _Target) -> BaseModel:
            resp = t.client.chat.completions.create(
                model=t.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                extra_body={"guided_json": schema},
            )
            raw = resp.choices[0].message.content
            return response_model.model_validate_json(raw)

        return self._run(role, fn)

    def call_with_tools(self, role: str, system: str, user: str, tools: list[dict]) -> tuple[str | None, list]:
        """Tool-calling de una sola vuelta. Devuelve (texto, tool_calls)."""
        return self.call_with_messages(
            role,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools,
        )

    def call_with_messages(self, role: str, messages: list[dict],
                           tools: list[dict]) -> tuple[str | None, list]:
        """Igual, pero sobre una conversación ya armada.

        Es lo que habilita encadenar tools: para pedir una segunda tool EN
        FUNCIÓN de lo que trajo la primera, el modelo tiene que volver a ver la
        conversación con los resultados adentro (mensajes `role: "tool"`). Con
        `[system, user]` fijo eso era imposible — el modelo tenía que decidir
        todas sus llamadas a ciegas, antes de ver ningún resultado.
        """
        def fn(t: _Target) -> tuple[str | None, list]:
            resp = t.client.chat.completions.create(
                model=t.model,
                messages=messages,
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
        def fn(t: _Target) -> str:
            resp = t.client.chat.completions.create(model=t.model, messages=messages, **kwargs)
            return resp.choices[0].message.content or ""

        return self._run(role, fn)

    # ── generación de imágenes ───────────────────────────────────────────────
    def tiene_rol(self, role: str) -> bool:
        """¿El rol está configurado DE VERDAD (alias existente con targets)?

        `_chain` es deliberadamente indulgente: un rol sin mapear cae al default
        para que una instancia vieja no se rompa cuando el motor agrega una
        capacidad. Para imágenes eso sería peor que fallar —terminaría pidiéndole
        un PNG a un modelo de texto—, así que quien genera pregunta primero.
        """
        alias = self._roles.get(role)
        return bool(alias and self._aliases.get(alias))

    def generate_image(self, role: str, prompt: str, *, size: str | None = None) -> bytes:
        """Genera una imagen y devuelve sus bytes. Misma cadena de fallback que el resto.

        Dos formas de API conviven porque los proveedores no se pusieron de acuerdo,
        y el hop las elige con `api` en la config:
          * `chat` (default) — chat/completions con `modalities: [image, text]`.
            Es lo que habla OpenRouter (Gemini, GPT-image).
          * `images` — el `/v1/images/generations` clásico. Es lo que hablan OpenAI,
            xAI (Grok) y casi cualquier servidor local.
        """
        if not self.tiene_rol(role):
            raise KeyError(f"rol {role!r} sin alias configurado (no hay modelo de imagen)")

        def fn(t: _Target) -> bytes:
            if str(t.opciones.get("api") or "chat").lower() == "images":
                return _bytes_de_payload(_imagen_por_endpoint_images(t, prompt, size))
            return _bytes_de_payload(_imagen_por_chat(t, prompt))

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

    def call_with_messages(self, messages: list[dict], tools: list[dict]) -> tuple[str | None, list]:
        return self._router.call_with_messages(self._role, messages, tools)

    def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return self._router.chat(self._role, messages, **kwargs)


def llm_api_key(env: Mapping[str, str] | None = None) -> str:
    """API key del LLM para los caminos que no declaran la suya: `LLM_API_KEY`
    (genérica — cualquier proveedor OpenAI-compatible) con `OPENROUTER_API_KEY`
    como alias de back-compat. Devuelve '' si no hay ninguna: los endpoints de
    la sección MODELS pueden traer su propia key (`api_key`/`api_key_env`), así
    que la ausencia recién es error donde la key se usa de verdad."""
    if env is None:
        import os
        env = os.environ
    return env.get("LLM_API_KEY") or env.get("OPENROUTER_API_KEY") or ""


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
            # Endpoint sin key declarada → la genérica del .env (LLM_API_KEY /
            # OPENROUTER_API_KEY). Permite que la UI prellene endpoints sin
            # obligar a decidir el nombre de la env var.
            endpoints[name] = {"base_url": cfg["base_url"],
                               "api_key": api_key or llm_api_key(env) or "x"}
            if "timeout_s" in cfg:
                endpoints[name]["timeout_s"] = cfg["timeout_s"]
        aliases = models_config["aliases"]
        # Los roles del motor se completan con los defaults y el settings de la
        # instancia gana. Sin esto, una instancia creada antes de que existiera
        # un rol nunca lo ve: anda igual (`_chain` cae al default) pero el admin
        # no lo encuentra en la UI para cambiarlo, que es peor que no tenerlo.
        roles = {**_DEFAULT_ROLES, **(models_config.get("roles") or {})}
    else:
        if not legacy.get("api_key"):
            raise SystemExit(
                "Falta la API key del LLM: seteá LLM_API_KEY (u OPENROUTER_API_KEY) en el "
                ".env de la instancia, o definí settings.json → MODELS.endpoints con "
                "api_key/api_key_env propios."
            )
        endpoints = {"default": {"base_url": legacy["base_url"], "api_key": legacy["api_key"]}}
        aliases = {
            "reasoning": [{"endpoint": "default", "model": legacy["reasoning"]}],
            "lite":      [{"endpoint": "default", "model": legacy["lite"]}],
            "vision":    [{"endpoint": "default", "model": legacy["vision"]}],
        }
        roles = dict(_DEFAULT_ROLES)

    return ModelRouter(endpoints, aliases, roles, max_retries=max_retries)
