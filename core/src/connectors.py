"""connectors.py — catálogo de CONECTORES de contenido (T44).

Un conector es "de dónde puede sacar contenido el bot": RSS, Membrilla, Spotify,
YouTube, Pinterest, Tumblr. Cada uno es un `type` posible de las entradas del
registro de fuentes (`config/sources.json`, T38b).

**Por qué existe este archivo:** el tipo de fuente estaba declarado en tres
lugares a la vez (motor, validador de la UI y JS de la UI) más su función de
fetch, así que sumar un conector era editar cuatro archivos y era fácil que
quedaran desincronizados. Acá vive la declaración; el resto la consume.

El módulo es **datos puros**: no importa botata ni nada pesado, así la UI de
configuración puede leer el catálogo sin levantar el motor. Las funciones de
fetch las inyecta botata al importarse (`register_fetcher`), porque necesitan sus
helpers (HTTP, log, catálogo).

Para sumar un conector nuevo: agregar un `Connector` a CONNECTORS, registrar su
fetcher desde botata y —si trae contenido nuevo— engancharlo a la tool que
corresponda. Nada más: la UI, el validador y los toggles salen de acá.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Connector:
    """Metadata de un conector. `logo` es un SVG inline (la UI es local y offline)."""
    id: str
    label: str
    color: str                     # color de marca, para el logo de la UI
    initial: str                   # letra/símbolo del logo
    placeholder: str               # ejemplo de cómo se escribe una fuente
    tool: str                      # qué tool consume estas fuentes
    help: str                      # una línea explicando qué trae
    needs_env: tuple[str, ...] = ()   # credenciales que hacen falta (aviso en la UI)
    live: bool = False             # True = trae lo último en vivo, sin indexar
    core: bool = False             # True = no se puede desactivar (lo usa el motor)


CONNECTORS: tuple[Connector, ...] = (
    Connector(
        id="membrilla", label="Membrilla", color="#c2410c", initial="🍐",
        placeholder="cuenta_una, board_dos",
        tool="search_images / search_videos",
        help="Contenido scrapeado e INDEXADO: se busca por significado.",
        core=True,
    ),
    Connector(
        id="rss", label="RSS", color="#ee802f", initial="◉",
        placeholder="https://diario.com/rss.xml",
        tool="get_news",
        help="Feeds de noticias. Titulares con su link.",
    ),
    Connector(
        id="spotify", label="Spotify", color="#1db954", initial="♫",
        placeholder="37i9dQZF1DX… (id de la playlist)",
        tool="get_playlist_track",
        help="Playlists: el bot comparte temas y suma las recomendaciones.",
        needs_env=("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"),
    ),
    Connector(
        id="youtube", label="YouTube", color="#ff0000", initial="▶",
        placeholder="@canal, UCxxxx (canal) o PLxxxx (lista)",
        tool="share_video",
        help="Canales y listas. Conserva la búsqueda abierta.",
        needs_env=("YOUTUBE_API_KEY",),
    ),
    Connector(
        id="pinterest", label="Pinterest", color="#e60023", initial="P",
        placeholder="usuario/tablero — o pegá la URL del tablero",
        tool="get_latest_media",
        help="Tableros por su RSS oficial. Trae lo último, sin indexar.",
        live=True,
    ),
    Connector(
        id="tumblr", label="Tumblr", color="#36465d", initial="t",
        placeholder="unblog, otro#etiqueta",
        tool="get_latest_media",
        help="Blogs vía API oficial. Trae lo último, sin indexar.",
        needs_env=("TUMBLR_API_KEY",),
        live=True,
    ),
)

_BY_ID = {c.id: c for c in CONNECTORS}

# Fetchers inyectados por botata (id → callable). Vacío si solo se importó la UI.
_FETCHERS: dict[str, Callable] = {}


def by_id(cid: str) -> Connector | None:
    return _BY_ID.get(cid)


def all_ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


def register_fetcher(cid: str, fn: Callable) -> None:
    """botata registra acá la función que trae contenido de ese conector."""
    _FETCHERS[cid] = fn


def fetcher(cid: str) -> Callable | None:
    return _FETCHERS.get(cid)


def is_enabled(cid: str, settings: dict) -> bool:
    """Un conector está activo salvo que el admin lo apague en `CONNECTORS`.

    Los `core=True` no se pueden apagar: el motor cuenta con ellos.
    """
    c = _BY_ID.get(cid)
    if c is None:
        return False
    if c.core:
        return True
    cfg = (settings or {}).get("CONNECTORS") or {}
    return bool((cfg.get(cid) or {}).get("enabled", True))


def enabled_ids(settings: dict) -> tuple[str, ...]:
    return tuple(c.id for c in CONNECTORS if is_enabled(c.id, settings))


def catalog(settings: dict) -> list[dict]:
    """Catálogo serializable para la UI (metadata + si está activo)."""
    return [
        {
            "id": c.id, "label": c.label, "color": c.color, "initial": c.initial,
            "placeholder": c.placeholder, "tool": c.tool, "help": c.help,
            "needs_env": list(c.needs_env), "live": c.live, "core": c.core,
            "enabled": is_enabled(c.id, settings),
        }
        for c in CONNECTORS
    ]
