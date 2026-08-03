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

import inspect
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any
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
    Connector(
        id="api", label="API (JSON)", color="#0ea5e9", initial="{}",
        placeholder="pikachu, bulbasaur — lo que cambia en la URL",
        tool="get_latest_media / get_data",
        help="Cualquier API que devuelva JSON: imágenes o datos (dólar, clima, "
             "fixture). Se configura acá, sin escribir código.",
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


def _accepts_entry(fn: Callable) -> bool:
    """¿El fetcher quiere la ENTRADA del registro además del nombre de la fuente?

    Los conectores clásicos (Pinterest, Tumblr, plugins) se bastan con el string
    de la fuente. Los declarativos necesitan la config de la entrada — url,
    mapeo—, que vive en `sources.json`. Se distingue por la firma para no romper
    los fetchers ya escritos ni obligar a los plugins a un parámetro que no usan.
    """
    try:
        return "entry" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def fetch_items(cid: str, source: str, limit: int = 15,
                entry: dict | None = None) -> list[dict]:
    """Trae items de una fuente. Único camino: nunca llamar al fetcher a mano.

    Devuelve [] ante cualquier falla — traer contenido es best-effort y una
    fuente rota no puede tumbar un pase del bot."""
    fn = _FETCHERS.get(cid)
    if fn is None:
        return []
    try:
        if _accepts_entry(fn):
            return fn(source, limit=limit, entry=entry) or []
        return fn(source, limit=limit) or []
    except Exception as e:
        log.warning("conector %s: falló al traer '%s': %s", cid, source, e)
        return []


# ─── El conector declarativo `api`: una API JSON descripta por config ────────
#
# Vive ACÁ y no en botata a propósito, contra la regla general del módulo: solo
# usa stdlib, así la UI de configuración puede ejecutarlo sin levantar el motor
# y ofrecer un botón de "probar". Sin esa prueba, alguien que no programa no
# tiene forma de saber por qué su fuente no trae nada.
_UA = "Mozilla/5.0 (compatible; botata/1.0)"
_MAX_BYTES = 2_000_000
# Lo único interpolable en la URL y los headers. Cerrado a propósito: el admin
# describe UNA API, no arma requests arbitrarios en runtime.
_TPL_RE = re.compile(r"\{(source|limit|env:[A-Za-z_][A-Za-z0-9_]*)\}")


class ApiSourceError(RuntimeError):
    """Config o respuesta inutilizable. El mensaje se le muestra tal cual al admin."""


def dig(data, path: str):
    """Camina un JSON con un path de puntos: `sprites.other.front_default`.

    Los índices numéricos entran a las listas (`results.0.name`). None si el
    camino no existe — que es distinto de existir valiendo null, pero para
    elegir un campo de una API la diferencia no cambia nada.
    """
    if not path:
        return data
    cur = data
    for parte in path.split("."):
        if isinstance(cur, list):
            if not parte.lstrip("-").isdigit():
                return None
            i = int(parte)
            cur = cur[i] if -len(cur) <= i < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(parte)
        else:
            return None
        if cur is None:
            return None
    return cur


def entry_env_vars(entry: dict) -> tuple[str, ...]:
    """Credenciales que pide una entrada `api` (para avisar si faltan en el .env)."""
    crudo = " ".join([
        str((entry or {}).get("url") or ""),
        *(f"{k}={v}" for k, v in ((entry or {}).get("headers") or {}).items()),
    ])
    return tuple(dict.fromkeys(
        m.group(1)[4:] for m in _TPL_RE.finditer(crudo) if m.group(1).startswith("env:")))


def _resolve_tpl(tpl: str, source: str, limit: int, quote: bool) -> str:
    faltan: list[str] = []

    def _sub(m: re.Match) -> str:
        token = m.group(1)
        if token == "limit":
            return str(limit)
        val = str(source) if token == "source" else (os.environ.get(token[4:]) or "")
        if token.startswith("env:") and not val:
            faltan.append(token[4:])
            return ""
        # En la URL va escapado: la fuente la escribe un humano en un campo de
        # texto y un espacio o un & romperían el request.
        return urllib.parse.quote(val, safe="") if quote else val

    out = _TPL_RE.sub(_sub, tpl or "")
    if faltan:
        raise ApiSourceError("falta en el .env: " + ", ".join(dict.fromkeys(faltan)))
    return out


def api_items(source: str, limit: int = 15, entry: dict | None = None) -> list[dict]:
    """Trae items de una API JSON descripta en la entrada del registro.

    La entrada declara:
        url        — endpoint, con {source} / {limit} / {env:MI_API_KEY}
        items_path — dónde está la lista en la respuesta ("" = la respuesta ES el item)
        map        — de qué campo sale cada cosa. Tres nombres tienen significado
                     para el motor (`image_url`, `url`, `title`) y el RESTO es
                     libre: `{"blue": "blue.venta", "oficial": "oficial.venta"}`
                     trae dos datos con esos nombres.
        headers    — opcional, mismos reemplazos (para APIs con token)

    Con `image_url` la fuente trae MEDIA (la consume `get_latest_media`); sin
    `image_url` trae DATOS (la consume `get_data`). Es la misma API JSON leída de
    dos maneras: no hay dos conectores, hay un mapeo que dice qué sacar. Antes
    `image_url` era obligatorio y eso dejaba afuera todo lo que no es una foto
    —el dólar, el clima, el fixture—, que en una comunidad es la mitad de lo que
    se habla.

    Levanta ApiSourceError con un mensaje explicable: lo lee el admin en la UI.
    """
    entry = entry or {}
    plantilla = str(entry.get("url") or "").strip()
    if not plantilla:
        raise ApiSourceError("la entrada no tiene URL")
    url = _resolve_tpl(plantilla, source, limit, quote=True)
    if not url.startswith(("http://", "https://")):
        raise ApiSourceError("la URL tiene que empezar con http:// o https://")
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    for k, v in (entry.get("headers") or {}).items():
        headers[str(k)] = _resolve_tpl(str(v), source, limit, quote=False)
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=15) as resp:
            crudo = resp.read(_MAX_BYTES)
    except Exception as e:
        raise ApiSourceError(f"la API no respondió: {e}") from e
    try:
        data = json.loads(crudo.decode("utf-8", "replace"))
    except Exception as e:
        raise ApiSourceError(f"la respuesta no es JSON: {e}") from e

    items_path = str(entry.get("items_path") or "").strip()
    nodo = dig(data, items_path)
    if nodo is None:
        raise ApiSourceError(
            (f"no encontré '{items_path}' en la respuesta. " + _listas_visibles(data))
            if items_path else "la respuesta vino vacía")
    if not items_path and (envoltorio := _clave_envoltorio(data)):
        # Solo con una clave que es envoltorio RECONOCIBLE. Chequear "¿hay
        # alguna lista?" a secas da falso positivo con cualquier item legítimo:
        # un pokémon trae `abilities`, `forms`, `game_indices`… y ninguna es la
        # lista de resultados.
        raise ApiSourceError(
            "dejaste vacío 'dónde está la lista', así que tomo la respuesta entera como "
            f"UN solo item, pero esta API devuelve una lista en '{envoltorio}'. "
            f"Poné '{envoltorio}' en 'dónde está la lista', o apuntá la URL a un item "
            "concreto en vez de al listado.")
    crudos = nodo if isinstance(nodo, list) else [nodo]

    mapa = {k: str(v).strip() for k, v in (entry.get("map") or {}).items()
            if str(v or "").strip()}
    if not mapa:
        raise ApiSourceError("falta decir qué campos traer (al menos uno)")
    img_path = mapa.get("image_url", "")
    # Campos libres: todo lo que no sea uno de los tres nombres reservados.
    libres = {k: v for k, v in mapa.items() if k not in ("image_url", "url", "title")}
    out: list[dict] = []
    for it in crudos:
        img = dig(it, img_path) if img_path else None
        if img_path and (not isinstance(img, str) or not img.startswith("http")):
            continue      # item sin imagen usable: se saltea, no es un error
        titulo = dig(it, mapa["title"]) if mapa.get("title") else ""
        link = dig(it, mapa["url"]) if mapa.get("url") else ""
        campos = {k: dig(it, ruta) for k, ruta in libres.items()}
        campos = {k: v for k, v in campos.items() if v is not None}
        if not img_path and not campos and not titulo:
            continue      # item que no trajo NADA de lo pedido
        out.append({"image_url": img if img_path else "",
                    "url": link if isinstance(link, str) else "",
                    "title": ("" if titulo is None else str(titulo))[:200],
                    "fields": campos,
                    "source": source})
        if len(out) >= max(1, int(limit)):
            break
    if not out:
        if img_path:
            raise ApiSourceError(
                f"traje {len(crudos)} resultado(s) pero ninguno tiene una imagen en "
                f"'{img_path}'. " + _campos_de_imagen(crudos[0]))
        raise ApiSourceError(
            f"traje {len(crudos)} resultado(s) pero ninguno tiene los campos que "
            f"pediste ({', '.join(sorted(mapa))}). " + _campos_visibles(crudos[0]))
    return out


# ─── Pistas para el admin (el botón "probar ahora" tiene que ENSEÑAR) ────────
# Sin esto, configurar una API es adivinar la forma de un JSON que no ves: el
# error decía qué faltaba pero no qué había. Estas dos funciones miran la
# respuesta REAL y sugieren los paths que existen — que es la diferencia entre
# "no anda" y "poné esto acá".
_CLAVES_LISTA = ("results", "items", "data", "posts", "photos", "entries", "hits", "docs")


def _clave_envoltorio(data: Any) -> str:
    """La clave donde esta API pone sus resultados, si es una de las conocidas."""
    if not isinstance(data, dict):
        return ""
    for k in _CLAVES_LISTA:
        v = data.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return k
    return ""


def _listas_visibles(data: Any, tope: int = 3) -> str:
    """Dónde hay listas en la respuesta, para llenar 'dónde está la lista'."""
    if not isinstance(data, dict):
        return ""
    hallados = [k for k, v in data.items() if isinstance(v, list) and v]
    hallados.sort(key=lambda k: k not in _CLAVES_LISTA)   # las conocidas primero
    if not hallados:
        return ""
    return ("Poné en 'dónde está la lista': " +
            " o ".join(f"'{k}'" for k in hallados[:tope]))


def _campos_visibles(item: Any, tope: int = 8) -> str:
    """Qué campos trae un item, para llenar el mapeo. El error de una fuente de
    DATOS tiene que decir qué había, no solo que no encontró lo que pidieron."""
    if not isinstance(item, dict):
        return ""
    claves = [k for k, v in item.items() if not isinstance(v, (list, dict))][:tope]
    return ("Campos disponibles: " + ", ".join(f"'{k}'" for k in claves)) if claves else ""


def _campos_de_imagen(item: Any, tope: int = 4) -> str:
    """Paths del item que parecen una imagen, para llenar 'campo de la imagen'."""
    encontrados: list[str] = []

    def recorrer(nodo: Any, prefijo: str, hondo: int) -> None:
        if len(encontrados) >= tope or hondo > 4:
            return
        if isinstance(nodo, dict):
            for k, v in nodo.items():
                recorrer(v, f"{prefijo}.{k}" if prefijo else str(k), hondo + 1)
        elif isinstance(nodo, str) and nodo.startswith("http") and any(
                nodo.lower().split("?")[0].endswith(e)
                for e in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
            encontrados.append(prefijo)

    recorrer(item, "", 0)
    if not encontrados:
        return "En este resultado no hay ningún campo que sea una URL de imagen."
    return "Campos con pinta de imagen: " + ", ".join(encontrados)


def _api_fetch(source: str, limit: int = 15, entry: dict | None = None) -> list[dict]:
    try:
        return api_items(source, limit, entry)
    except ApiSourceError as e:
        log.warning("api: fuente '%s': %s", source, e)
        return []


_FETCHERS["api"] = _api_fetch


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
