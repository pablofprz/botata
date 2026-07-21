"""Tests de la tool web_search (T8). El GET a Brave va mockeado (sin red)."""
from __future__ import annotations

import os
import sys
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
