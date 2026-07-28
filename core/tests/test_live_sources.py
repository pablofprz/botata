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
<description>&lt;a&gt;&lt;img src="https://i.pinimg.com/a.jpg"/&gt;&lt;/a&gt;</description></item>
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
    assert items[0]["image_url"] == "https://i.pinimg.com/a.jpg"
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
    assert "no tengo tableros ni blogs para 'cocina'" in out.text
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
    assert "no hay fuentes de Pinterest ni Tumblr" in out.text


def test_scope_no_es_publico():
    """El conector baja archivos: jamás scope reply (cualquiera lo dispararía)."""
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "get_latest_media" not in [t.name for t in reg.available(Scope.REPLY)]
    assert "get_latest_media" in [t.name for t in reg.available(Scope.ADMIN)]
