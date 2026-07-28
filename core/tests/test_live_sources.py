"""T42 · Conectores en vivo: Pinterest (RSS del tablero) y Tumblr (API v2).

Traen FRESCURA ("lo último de este tablero"), no búsqueda temática: lo que baja
no está descripto por el modelo de visión y no entra al catálogo. Esa frontera
es la razón de que Membrilla siga existiendo — son usos distintos.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_PIN_RSS = """<rss><channel>
<item><title>Un pin lindo</title><link>https://pinterest.com/pin/1</link>
<description>&lt;a&gt;&lt;img src="https://i.pinimg.com/236x/ab/cd/ef/abcdef.jpg"/&gt;&lt;/a&gt;</description></item>
<item><title>Sin imagen</title><link>https://pinterest.com/pin/2</link>
<description>texto pelado</description></item>
</channel></rss>"""

_TUMBLR_JSON = {"response": {"posts": [
    {"post_url": "https://blog.tumblr.com/post/1", "summary": "una foto",
     "photos": [{"original_size": {"url": "https://64.media.tumblr.com/a.jpg"}}]},
    {"post_url": "https://blog.tumblr.com/post/2", "summary": "sin fotos", "photos": []},
]}}


class _Resp:
    def __init__(self, data: bytes): self._d = data
    def read(self, n=None): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ─── Pinterest: RSS oficial del tablero, sin credenciales ───────────────────
def test_pinterest_arma_la_url_del_tablero(monkeypatch):
    vistas = []

    def _open(req, timeout=15):
        vistas.append(req.full_url)
        return _Resp(_PIN_RSS.encode())
    monkeypatch.setattr(b.urllib.request, "urlopen", _open)
    b._pinterest_board_items("polci/memes")
    assert vistas == ["https://www.pinterest.com/polci/memes.rss"]


def test_pinterest_extrae_la_imagen_y_saltea_lo_que_no_tiene(monkeypatch):
    monkeypatch.setattr(b.urllib.request, "urlopen",
                        lambda req, timeout=15: _Resp(_PIN_RSS.encode()))
    items = b._pinterest_board_items("polci/memes")
    assert len(items) == 1                      # el segundo item no tiene <img>
    assert items[0]["image_url"] == "https://i.pinimg.com/736x/ab/cd/ef/abcdef.jpg"
    assert items[0]["url"] == "https://pinterest.com/pin/1"


def test_pinterest_error_de_red_no_rompe(monkeypatch):
    def _boom(req, timeout=15):
        raise RuntimeError("pinterest caído")
    monkeypatch.setattr(b.urllib.request, "urlopen", _boom)
    assert b._pinterest_board_items("x/y") == []


# ─── Tumblr: API v2 con consumer key ────────────────────────────────────────
def test_tumblr_sin_key_no_explota(monkeypatch):
    monkeypatch.delenv("TUMBLR_API_KEY", raising=False)
    assert b._tumblr_blog_items("unblog") == []


def test_tumblr_normaliza_host_y_tag(monkeypatch):
    monkeypatch.setenv("TUMBLR_API_KEY", "k")
    vistas = []

    def _open(req, timeout=15):
        vistas.append(req.full_url)
        return _Resp(json.dumps(_TUMBLR_JSON).encode())
    monkeypatch.setattr(b.urllib.request, "urlopen", _open)
    b._tumblr_blog_items("unblog#gatos")
    assert "unblog.tumblr.com" in vistas[0] and "tag=gatos" in vistas[0]
    assert "type=photo" in vistas[0]


def test_tumblr_saca_la_foto_original(monkeypatch):
    monkeypatch.setenv("TUMBLR_API_KEY", "k")
    monkeypatch.setattr(b.urllib.request, "urlopen",
                        lambda req, timeout=15: _Resp(json.dumps(_TUMBLR_JSON).encode()))
    items = b._tumblr_blog_items("unblog")
    assert len(items) == 1                      # el post sin fotos se saltea
    assert items[0]["image_url"] == "https://64.media.tumblr.com/a.jpg"


# ─── la tool get_latest_media ───────────────────────────────────────────────
@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "live.db")


@pytest.fixture()
def registro(monkeypatch, tmp_path):
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "pinterest", "name": "tableros", "category": "ilustracion",
         "sources": ["polci/ilustra"], "enabled": True},
        {"type": "tumblr", "name": "blogs", "category": "gatos",
         "sources": ["gatoblog"], "enabled": True},
    ])
    monkeypatch.setattr(b, "_descargar_media", lambda url: str(tmp_path / "bajada.jpg"))


def test_tool_trae_de_la_fuente_del_tema(conn, registro, monkeypatch):
    monkeypatch.setattr(b, "_pinterest_board_items",
                        lambda ref, limit=15: [{"image_url": "u", "url": "https://pin/1",
                                                "title": "dibujo", "source": ref}])
    monkeypatch.setattr(b, "_tumblr_blog_items",
                        lambda ref, limit=15: pytest.fail("no debería mirar tumblr"))
    out = b._tool_get_latest_media({"topic": "ilustracion"}, ToolContext(state={}, conn=conn))
    assert out.image_path and "dibujo" in out.text


def test_tool_tema_desconocido_sugiere(conn, registro):
    out = b._tool_get_latest_media({"topic": "cocina"}, ToolContext(state={}, conn=conn))
    # El texto habla de "fuentes en vivo" y no de Pinterest/Tumblr: desde el
    # conector `api` y los plugins, la lista de en-vivo ya no es fija.
    assert "no tengo fuentes en vivo para 'cocina'" in out.text
    assert "ilustracion" in out.text and "gatos" in out.text
    assert out.image_path is None


def test_tool_evita_repetir_lo_ya_posteado(conn, registro, monkeypatch):
    b.log_bot_post(conn, uri="at://b/1", in_reply_to=None, reply_to_handle=None,
                   text="miren esto https://pin/viejo")
    monkeypatch.setattr(b, "_pinterest_board_items", lambda ref, limit=15: [
        {"image_url": "u1", "url": "https://pin/viejo", "title": "repetido", "source": ref},
        {"image_url": "u2", "url": "https://pin/nuevo", "title": "fresco", "source": ref},
    ])
    out = b._tool_get_latest_media({"topic": "ilustracion"}, ToolContext(state={}, conn=conn))
    assert "fresco" in out.text


def test_tool_sin_fuentes_configuradas(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(b, "SOURCES", [])
    out = b._tool_get_latest_media({}, ToolContext(state={}, conn=conn))
    assert "no hay fuentes en vivo configuradas" in out.text


def test_disponible_en_replies():
    """2026-07-27: pasó a scope reply. Un usuario pide "una foto de un mapache" y
    el bot tiene que poder traerla de las fuentes que el admin declaró — antes la
    tool existía pero era inalcanzable desde una mención, que es de donde vienen
    los pedidos."""
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    disponibles = [t.name for t in reg.available(Scope.REPLY)]
    assert "get_latest_media" in disponibles
    assert "get_latest_media" in [t.name for t in reg.available(Scope.ADMIN)]


# ─── Pinterest: cómo se escribe el tablero (el admin pega cualquier cosa) ────
@pytest.mark.parametrize("escrito", [
    "polci/memes",
    "https://www.pinterest.com/polci/memes",
    "https://www.pinterest.com/polci/memes/",
    "https://www.pinterest.com/polci/memes.rss",
    "  polci/memes/  ",
])
def test_pinterest_normaliza_todas_las_formas(monkeypatch, escrito):
    """Pegar la URL del navegador tenía que funcionar igual que 'usuario/tablero':
    sin normalizar traía el HTML de la página y el parser devolvía vacío."""
    vistas = []

    def _open(req, timeout=15):
        vistas.append(req.full_url)
        return _Resp(_PIN_RSS.encode())
    monkeypatch.setattr(b.urllib.request, "urlopen", _open)
    b._pinterest_board_items(escrito)
    assert vistas == ["https://www.pinterest.com/polci/memes.rss"]


def test_pinterest_sube_la_resolucion_con_fallback(monkeypatch):
    monkeypatch.setattr(b.urllib.request, "urlopen",
                        lambda req, timeout=15: _Resp(_PIN_RSS.encode()))
    it = b._pinterest_board_items("polci/memes")[0]
    assert "/736x/" in it["image_url"]          # el RSS sirve 236x: se ve mal posteado
    assert "/236x/" in it["image_url_alt"]      # y queda la miniatura por las dudas


def test_tool_cae_a_la_miniatura_si_falla_el_grande(conn, registro, monkeypatch):
    intentos = []

    def _bajar(url):
        intentos.append(url)
        return None if "/736x/" in url else "/tmp/chica.jpg"
    monkeypatch.setattr(b, "_descargar_media", _bajar)
    monkeypatch.setattr(b, "_pinterest_board_items", lambda ref, limit=15: [
        {"image_url": "https://i.pinimg.com/736x/a.jpg",
         "image_url_alt": "https://i.pinimg.com/236x/a.jpg",
         "url": "https://pin/1", "title": "x", "source": ref}])
    out = b._tool_get_latest_media({"topic": "ilustracion"}, ToolContext(state={}, conn=conn))
    assert len(intentos) == 2 and out.image_path == "/tmp/chica.jpg"


# ═══ Pin suelto (no solo tableros) ═══════════════════════════════════════════
def test_pin_suelto_sale_por_opengraph(monkeypatch):
    """Un PIN no tiene RSS: devuelve 200 con CERO items (falla mudo). Su página
    sí trae og:image, así que se resuelve por OpenGraph."""
    vistas = []

    def _og(url, max_bytes=200_000):
        vistas.append((url, max_bytes))
        return {"title": "Pin on Raccoons", "description": "",
                "image": "https://i.pinimg.com/736x/ab/cd/ef/pin.jpg"}
    monkeypatch.setattr(b, "_fetch_og_card", _og)
    items = b._pinterest_board_items("https://ar.pinterest.com/pin/1055599909111921/")
    assert items[0]["image_url"] == "https://i.pinimg.com/736x/ab/cd/ef/pin.jpg"
    assert items[0]["title"] == "Pin on Raccoons"
    # el og:image del pin vive pasado el byte 1.100.000: hay que pedir más
    assert vistas[0][1] >= 1_200_000


def test_pin_sin_imagen_no_rompe(monkeypatch):
    monkeypatch.setattr(b, "_fetch_og_card", lambda url, max_bytes=200_000: None)
    assert b._pinterest_board_items("https://ar.pinterest.com/pin/1/") == []


def test_el_tablero_sigue_yendo_por_rss(monkeypatch):
    """Los tableros no deben pasar por OpenGraph."""
    monkeypatch.setattr(b, "_fetch_og_card",
                        lambda *a, **k: pytest.fail("un tablero no usa OpenGraph"))
    monkeypatch.setattr(b.urllib.request, "urlopen",
                        lambda req, timeout=15: _Resp(_PIN_RSS.encode()))
    assert b._pinterest_board_items("polci/memes")


# ═══ El pedido de imagen usa las fuentes en vivo, no solo el catálogo ════════
def test_tema_sin_indexar_cae_en_las_fuentes_en_vivo(conn, registro, monkeypatch):
    """El caso real: el tema 'mapaches' solo tiene un pin de Pinterest. Antes
    search_images contestaba "no tengo fuentes" aunque estuviera declarado."""
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "pinterest", "category": "mapaches",
         "sources": ["https://ar.pinterest.com/pin/1/"], "enabled": True}])
    monkeypatch.setattr(b, "_pinterest_board_items", lambda ref, limit=15: [
        {"image_url": "https://i.pinimg.com/736x/x.jpg", "url": "https://pin/1",
         "title": "un mapache", "source": ref}])
    out = b._tool_search_images({"query": "mapache", "topic": "mapaches"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path and "mapache" in out.text


def test_un_tema_indexado_puede_ir_igual_a_las_fuentes_en_vivo(conn, monkeypatch, tmp_path):
    """Ya NO manda el catálogo por tener algo indexado.

    Antes (2026-07-27) el catálogo tenía prioridad fija. El admin la derogó
    (2026-07-28): *"search_images debería poder buscar de Membrilla pero también
    de las fuentes asociadas sin ninguna prioridad"*. Con los dos sirviendo,
    quién gana lo decide `source_weight` — acá se fuerza la moneda para que el
    test sea determinista."""
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "membrilla", "category": "memes", "sources": ["cuenta"], "enabled": True},
        {"type": "pinterest", "category": "memes", "sources": ["u/b"], "enabled": True}])
    monkeypatch.setattr(b.dbmod, "prefer_fresh_media", lambda rows: rows)
    monkeypatch.setattr(b.dbmod, "hybrid_search_image_catalog",
                        lambda *a, **k: [{"id": 1, "file_path": "scrape/x.jpg",
                                          "description": "un meme", "category": "meme"}])
    monkeypatch.setattr(b, "_postable_media_path", lambda p: "/abs/" + p)
    monkeypatch.setattr(b.dbmod, "mark_image_used", lambda *a: None)
    monkeypatch.setattr(b, "_traer_de_fuentes_en_vivo",
                        lambda *a, **k: b.ToolResult(text="de u/b: pin", image_path="/abs/pin.jpg"))

    monkeypatch.setattr(b.random, "random", lambda: 0.99)      # cae del lado del catálogo
    assert b._tool_search_images({"query": "meme", "topic": "memes"},
                                 ToolContext(state={}, conn=conn)).image_path == "/abs/scrape/x.jpg"

    monkeypatch.setattr(b.random, "random", lambda: 0.0)       # cae del lado de la fuente
    assert b._tool_search_images({"query": "meme", "topic": "memes"},
                                 ToolContext(state={}, conn=conn)).image_path == "/abs/pin.jpg"


def test_tema_que_no_existe_en_ningun_lado_avisa(conn, registro):
    out = b._tool_search_images({"query": "x", "topic": "dinosaurios"},
                                ToolContext(state={}, conn=conn))
    assert "no tengo fuentes declaradas" in out.text and out.image_path is None
