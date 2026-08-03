"""Tests de la tool web_search (T8). El GET a Brave va mockeado (sin red)."""
from __future__ import annotations

import os
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_CTX = ToolContext(state={}, conn=None)
_FAKE = [
    {"title": "Python 3.13", "url": "https://python.org", "description": "release notes"},
    {"title": "Docs", "url": "https://docs.python.org", "description": "la doc oficial"},
]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    # Fija la key para no depender del .env real; los tests de red van mockeados.
    monkeypatch.setattr(b, "BRAVE_API_KEY", "test-key")


def test_formats_results(monkeypatch):
    monkeypatch.setattr(b, "_brave_search", lambda q, c=5: _FAKE)
    out = b._tool_web_search({"query": "python 3.13"}, _CTX).text
    assert "Python 3.13" in out and "https://python.org" in out
    assert "la doc oficial" in out


def test_no_results(monkeypatch):
    monkeypatch.setattr(b, "_brave_search", lambda q, c=5: [])
    assert "no encontré" in b._tool_web_search({"query": "asdfqwer"}, _CTX).text


def test_empty_query(monkeypatch):
    monkeypatch.setattr(b, "_brave_search", lambda q, c=5: _FAKE)
    assert "necesito algo" in b._tool_web_search({"query": "  "}, _CTX).text


def test_error_is_graceful(monkeypatch):
    def _boom(q, c=5):
        raise RuntimeError("network down")
    monkeypatch.setattr(b, "_brave_search", _boom)
    assert "no pude buscar" in b._tool_web_search({"query": "x"}, _CTX).text


def test_missing_key_is_graceful(monkeypatch):
    monkeypatch.setattr(b, "BRAVE_API_KEY", None)
    assert "no está configurada" in b._tool_web_search({"query": "x"}, _CTX).text


def test_count_is_clamped(monkeypatch):
    captured = {}
    def _spy(q, c=5):
        captured["count"] = c
        return _FAKE
    monkeypatch.setattr(b, "_brave_search", _spy)
    b._tool_web_search({"query": "x", "count": 99}, _CTX)
    assert captured["count"] == 10  # máx 10


def test_scopes_default():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    for sc in (Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN):
        assert "web_search" in [t.name for t in reg.available(sc)]


# ─── 429: el tier gratuito de Brave es ~1 req/s y el loop de tools busca 2 veces ──
def _http429():
    return urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)


def test_429_reintenta_una_vez(monkeypatch):
    intentos = []
    def _flaky(q, c=5):
        intentos.append(q)
        if len(intentos) == 1:
            raise _http429()
        return _FAKE
    monkeypatch.setattr(b, "_brave_search", _flaky)
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    out = b._tool_web_search({"query": "dólar blue"}, _CTX).text
    assert len(intentos) == 2 and "Python 3.13" in out


def test_429_persistente_avisa(monkeypatch):
    monkeypatch.setattr(b, "_brave_search", lambda q, c=5: (_ for _ in ()).throw(_http429()))
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    assert "limitado" in b._tool_web_search({"query": "x"}, _CTX).text


def test_otro_http_no_reintenta(monkeypatch):
    intentos = []
    def _500(q, c=5):
        intentos.append(q)
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)
    monkeypatch.setattr(b, "_brave_search", _500)
    assert "no pude buscar" in b._tool_web_search({"query": "x"}, _CTX).text
    assert len(intentos) == 1
