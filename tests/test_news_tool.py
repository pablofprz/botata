"""Tests de la tool get_news (T15, reply): usuario pide noticias/links por categoría."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import butterbot as b  # noqa: E402
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


def test_independent_of_news_enabled(monkeypatch):
    # get_news no depende de NEWS_ENABLED (que es solo para el posteo autónomo)
    monkeypatch.setattr(b, "NEWS_ENABLED", False)
    assert "titular" in b._tool_get_news({}, _CTX).text


def test_scopes_default():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "get_news" in [t.name for t in reg.available(Scope.REPLY)]
    assert "get_news" in [t.name for t in reg.available(Scope.ADMIN)]
    # y el cambio de calendario: create_event ahora también en reply
    assert "create_event" in [t.name for t in reg.available(Scope.REPLY)]
