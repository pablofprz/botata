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

import logging
from dataclasses import dataclass, fields, replace
from typing import Callable

log = logging.getLogger("botata.connectors")


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


_BUILTIN: tuple[Connector, ...] = (
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

# Los built-in más los que se sumen en runtime: plugins de la instancia
# (connectors/*.py) y servers MCP que declaren ser conectores. La lista es
# mutable a propósito — es el punto de extensión del sistema.
CONNECTORS: list[Connector] = list(_BUILTIN)
_BY_ID: dict[str, Connector] = {c.id: c for c in _BUILTIN}

# Fetchers inyectados por botata (id → callable). Vacío si solo se importó la UI.
_FETCHERS: dict[str, Callable] = {}

# Ids que vinieron de connectors/*.py, para poder recargarlos.
_PLUGIN_IDS: set[str] = set()


def by_id(cid: str) -> Connector | None:
    return _BY_ID.get(cid)


def all_ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


def register(conn: Connector, fetch: Callable | None = None) -> None:
    """Suma (o reemplaza) un conector en runtime. Devuelve nada; idempotente."""
    if conn.core and conn.id not in _BY_ID:
        conn = replace(conn, core=False)     # solo los built-in pueden ser core
    if conn.id in _BY_ID:
        CONNECTORS[[c.id for c in CONNECTORS].index(conn.id)] = conn
    else:
        CONNECTORS.append(conn)
    _BY_ID[conn.id] = conn
    if fetch is not None:
        _FETCHERS[conn.id] = fetch


def unregister(cid: str) -> bool:
    """Saca un conector agregado en runtime. Los built-in no se tocan."""
    con = _BY_ID.get(cid)
    if con is None or con in _BUILTIN:
        return False
    CONNECTORS.remove(con)
    _BY_ID.pop(cid, None)
    _FETCHERS.pop(cid, None)
    return True


def register_fetcher(cid: str, fn: Callable) -> None:
    """botata registra acá la función que trae contenido de ese conector."""
    _FETCHERS[cid] = fn


# ─── Extensión 1: plugins de la instancia (connectors/*.py) ─────────────────
def load_instance_plugins(plugins_dir) -> list[str]:
    """Carga conectores sueltos de `<instancia>/connectors/*.py`.

    Cada archivo declara un dict `CONNECTOR` con los campos de `Connector` y una
    función `fetch(source, limit=15) -> list[dict]` que devuelve items con
    `image_url`/`url`/`title` (el mismo contrato que los conectores en vivo).

    ⚠️ Esto IMPORTA Y EJECUTA el archivo: es código con acceso a las credenciales
    y a la DB del bot. Sirve para los plugins del propio admin. Si algún día hay
    plugins de terceros, esta puerta necesita otra cosa (manifiesto declarativo o
    MCP, que corre en otro proceso) — ver T44 fase 2 en el ROADMAP.
    """
    import importlib.util
    from pathlib import Path

    d = Path(plugins_dir)
    # Recargable: se olvidan los de la pasada anterior, así borrar un archivo
    # hace desaparecer el conector (la UI relee el catálogo en cada request).
    for viejo in _PLUGIN_IDS:
        unregister(viejo)
    _PLUGIN_IDS.clear()
    if not d.is_dir():
        return []
    cargados = []
    for archivo in sorted(d.glob("*.py")):
        if archivo.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"botata_plugin_{archivo.stem}", archivo)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            meta = dict(getattr(mod, "CONNECTOR", {}) or {})
            fetch = getattr(mod, "fetch", None)
            if not meta.get("id") or not callable(fetch):
                log.warning("plugin %s: le falta CONNECTOR['id'] o la función fetch()", archivo.name)
                continue
            campos = {f.name for f in fields(Connector)}
            register(Connector(**{k: v for k, v in meta.items() if k in campos}), fetch)
            cargados.append(meta["id"])
            _PLUGIN_IDS.add(meta["id"])
        except Exception as e:
            log.error("plugin %s no cargó: %s", archivo.name, e)
    if cargados:
        log.info("conectores de la instancia: %s", ", ".join(cargados))
    return cargados


# ─── Extensión 2: un server MCP que además es fuente ────────────────────────
def register_from_mcp(server: str, cfg: dict, call: Callable) -> str | None:
    """Registra un conector servido por un MCP, si el server lo declara.

    En `settings.json → MCP → <server> → connector` el admin describe cómo usar
    una tool del server como fuente de contenido:

        "connector": {"id": "lemmy", "label": "Lemmy", "color": "#00bc8c",
                      "initial": "L", "placeholder": "comunidad@instancia",
                      "fetch_tool": "get_posts", "arg": "community", "live": true}

    Así **cualquier** server MCP existente puede volverse un conector sin que su
    autor haga nada: el mapeo es config del admin. `call(tool, args)` es el proxy
    que inyecta el cliente MCP.
    """
    meta = dict((cfg or {}).get("connector") or {})
    cid, tool = meta.get("id"), meta.get("fetch_tool")
    if not cid or not tool:
        return None
    arg_name = meta.get("arg", "source")
    campos = {f.name for f in fields(Connector)}
    datos = {k: v for k, v in meta.items() if k in campos}
    datos.setdefault("label", cid.capitalize())
    datos.setdefault("color", "#6b7280")
    datos.setdefault("initial", cid[:1].upper())
    datos.setdefault("placeholder", "identificador de la fuente")
    datos.setdefault("tool", "get_latest_media")
    datos.setdefault("help", f"Contenido servido por el MCP '{server}'.")
    datos.setdefault("live", True)

    def _fetch(source: str, limit: int = 15) -> list[dict]:
        try:
            return call(tool, {arg_name: source, "limit": limit}) or []
        except Exception as e:
            log.warning("conector MCP %s: falló %s(%s): %s", cid, tool, source, e)
            return []

    register(Connector(**datos), _fetch)
    return cid


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
