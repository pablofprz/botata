"""Tests de la tool get_news (T15): noticias por categoría, y modo only_new
(dedup persistente) para rutinas. El posteo autónomo de noticias ya no existe
como tarea: es una rutina que llama a esta tool."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_CTX = ToolContext(state={"author_handle": "u"}, conn=None)

_SOURCES = [
    {"url": "http://lpo", "host": "lpo", "title": "La Política Online", "category": "política", "enabled": True},
    {"url": "http://p12", "host": "p12", "title": "Página/12", "category": "noticias", "enabled": True},
    {"url": "http://off", "host": "off", "title": "Apagada", "category": "deportes", "enabled": False},
]


def _fake_rss(url, max_items=3):
    return [{"title": f"titular de {url}", "link": f"{url}/1", "description": "d", "id": f"{url}/1"}]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(b, "NEWS_SOURCES", _SOURCES)
    monkeypatch.setattr(b, "fetch_rss", _fake_rss)


# ─── fetch_rss: parseo RSS 2.0 real (stdlib) ──────────────────────────────────
class _FakeResp:
    def __init__(self, data): self._data = data
    def read(self): return self._data
    def __enter__(self): return self
    def __exit__(self, *a): return False


_RSS = (
    b"<rss><channel>"
    b"<item><title>Titular 1</title><link>http://n/1</link>"
    b"<description>&lt;p&gt;bajada uno&lt;/p&gt;</description></item>"
    b"<item><title>Titular 2</title><link>http://n/2</link>"
    b"<description>bajada dos</description></item>"
    b"</channel></rss>"
)


# referencia a la función real ANTES de que el fixture autouse la mockee
_REAL_FETCH_RSS = b.fetch_rss


def test_fetch_rss_parses_and_strips_html(monkeypatch):
    monkeypatch.setattr(b.urllib.request, "urlopen", lambda req, timeout=15: _FakeResp(_RSS))
    items = _REAL_FETCH_RSS("http://feed")
    assert len(items) == 2
    assert items[0] == {"title": "Titular 1", "link": "http://n/1",
                        "description": "bajada uno", "id": "http://n/1"}


def test_returns_from_all_enabled_sources():
    out = b._tool_get_news({}, _CTX).text
    assert "La Política Online" in out and "Página/12" in out
    assert "http://lpo/1" in out
    assert "Apagada" not in out  # fuente deshabilitada


def test_filter_by_category():
    out = b._tool_get_news({"category": "política"}, _CTX).text
    assert "La Política Online" in out
    assert "Página/12" not in out


def test_unknown_category_is_graceful():
    assert "no tengo fuentes de noticias de 'cocina'" in b._tool_get_news({"category": "cocina"}, _CTX).text


def test_no_sources_configured(monkeypatch):
    monkeypatch.setattr(b, "NEWS_SOURCES", [])
    assert "no tengo fuentes" in b._tool_get_news({}, _CTX).text


def test_fetch_error_is_graceful(monkeypatch):
    def _boom(url, max_items=3):
        raise RuntimeError("rss down")
    monkeypatch.setattr(b, "fetch_rss", _boom)
    assert "no pude traer noticias" in b._tool_get_news({}, _CTX).text


# ─── only_new: dedup persistente para rutinas ────────────────────────────────
@pytest.fixture()
def ctx_db(tmp_path):
    conn = d.init_db(tmp_path / "news_tool.db")
    return ToolContext(state={"author_handle": "u"}, conn=conn)


def test_only_new_marca_y_no_repite(ctx_db):
    out1 = b._tool_get_news({"only_new": True}, ctx_db).text
    assert "titular" in out1
    out2 = b._tool_get_news({"only_new": True}, ctx_db).text  # mismos items → nada
    assert "no hay titulares nuevos" in out2


def test_only_new_no_afecta_al_modo_normal(ctx_db):
    b._tool_get_news({"only_new": True}, ctx_db)   # marca todo visto
    out = b._tool_get_news({}, ctx_db).text        # modo normal: igual los muestra
    assert "titular" in out


def test_sin_only_new_no_marca(ctx_db):
    b._tool_get_news({}, ctx_db)
    assert not d.news_item_posted(ctx_db.conn, "http://lpo/1")


def test_scopes_default():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "get_news" in [t.name for t in reg.available(Scope.REPLY)]
    assert "get_news" in [t.name for t in reg.available(Scope.ADMIN)]
    # 2026-07-27: get_news disponible en rutinas (scope feed_reflection)
    assert "get_news" in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
    # y el cambio de calendario: create_event ahora también en reply
    assert "create_event" in [t.name for t in reg.available(Scope.REPLY)]
