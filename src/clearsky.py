"""clearsky.py — cliente de la API pública de ClearSky.

ClearSky indexa los registros `app.bsky.graph.block` del firehose de Bluesky y
expone un índice reverso ("quién bloquea a X") que ATProto no provee nativamente.
Esta es la fuente de la funcionalidad de consulta de bloques de botata (v1).

API confirmada en vivo (2026-07-07):
    GET {BASE}/blocklist/{DID}/{page}
    → {"data": {"blocklist": [{"did": "...", "blocked_date": "..."}] | null},
       "identity": "...", "status": true}

Paginación por número de página (1-based); páginas más allá de la última
devuelven `blocklist: null`. Sin cursor. La variant query-style ya no existe.

Ver CLAUDE.md → "Esquema de persistencia" y la decisión de proxy vs indexador
propio (fase 2: indexador Jetstream swappea esta fuente sin tocar la node).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("botata.clearsky")

# Host público actual (anunciado 11/2025; el viejo `api.clearsky.services` está deprecado).
CLEARSKY_BASE = "https://public.api.clearsky.services/api/v1/anon"
_TIMEOUT_SECONDS = 10
MAX_PAGES = 10  # safety cap — evita iterar indefinidamente si ClearSky cambia de schema


class ClearSkyError(RuntimeError):
    """Fallo de red / HTTP 5xx / parseo contra ClearSky. La node la atrapa."""


def _fetch_page(did: str, page: int) -> list[dict[str, str]] | None:
    """Devuelve la lista de bloqueadores de una página, o None si no hay más páginas."""
    url = f"{CLEARSKY_BASE}/blocklist/{did}/{page}"
    log.debug("ClearSky GET %s", url)
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310 — URL construida con DID validado abajo
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # 404 / 400 = el DID no existe en el índice o ClearSky no lo conoce → sin bloqueadores.
        if e.code in (400, 404):
            return []
        raise ClearSkyError(f"ClearSky HTTP {e.code} para {did}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ClearSkyError(f"ClearSky inalcanzable para {did}: {e}") from e

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as e:
        raise ClearSkyError(f"ClearSky: respuesta no-JSON para {did}: {e}") from e

    if not payload.get("status", False):
        raise ClearSkyError(f"ClearSky: status=false para {did}")

    data = payload.get("data") or {}
    blocklist = data.get("blocklist")
    if blocklist is None:
        return None  # fin de la paginación
    if not isinstance(blocklist, list):
        raise ClearSkyError(f"ClearSky: blocklist inesperado para {did}")
    return blocklist


def get_blocked_by(did: str) -> list[dict[str, str]]:
    """Cuentas que bloquean a `did`, según ClearSky.

    Devuelve una lista de dicts `{"did": str, "blocked_date": str}`. Vacía si nadie
    lo bloquea o si ClearSky no tiene indexado al DID. Lanza `ClearSkyError` ante
    fallos de red/servidor para que la caller decida el reply de fallback.
    """
    if not did.startswith("did:"):
        raise ClearSkyError(f"DID inválido: {did!r}")

    blockers: list[dict[str, str]] = []
    for page in range(1, MAX_PAGES + 1):
        chunk = _fetch_page(did, page)
        if chunk is None:
            break  # blocklist: null → no hay más páginas
        blockers.extend(chunk)
        if len(chunk) == 0:
            break  # página vacía (DID no indexado)
    return blockers
