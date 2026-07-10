"""Tests de la tool `summarize_feed` (scope reply, on-demand) y su toggle por config."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import butterbot as b  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402


class FakeBsky:
    def __init__(self, posts):
        self._posts = posts

    def get_list_feed(self, uri, since=None, limit=50):
        return self._posts

    def get_feed_posts(self, source_type, identifier, since=None, limit=50):
        return self._posts


class FakeRouter:
    def __init__(self, reply="Se habló del dólar y de una peli."):
        self._reply = reply

    def chat(self, role, messages, **kw):
        return self._reply


FEED = [{"name": "polcifeed", "uri": "at://x", "enabled": True}]


@pytest.fixture(autouse=True)
def _feeds(monkeypatch):
    monkeypatch.setattr(b, "FEEDS_CONFIG", FEED)


def _ctx():
    return ToolContext(state={"author_handle": "u", "mention_text": "resumime el feed"}, conn=None)


def test_registered_in_reply_scope_only():
    reg = b.build_tool_registry(b.TOOLS_CONFIG, bsky=FakeBsky([]), router=FakeRouter())
    assert "summarize_feed" in [t.name for t in reg.available(Scope.REPLY)]
    assert "summarize_feed" not in [t.name for t in reg.available(Scope.ADMIN)]


def test_returns_summary():
    posts = [{"handle": "a", "text": "hoy el dólar"}, {"handle": "b", "text": "vi una peli"}]
    reg = b.build_tool_registry(b.TOOLS_CONFIG, bsky=FakeBsky(posts), router=FakeRouter())
    res = reg.execute("summarize_feed", {}, _ctx())
    assert "dólar" in res.text
    assert res.image_path is None


def test_empty_feed_is_graceful():
    reg = b.build_tool_registry(b.TOOLS_CONFIG, bsky=FakeBsky([]), router=FakeRouter())
    res = reg.execute("summarize_feed", {}, _ctx())
    assert "tranquilo" in res.text or "movimiento" in res.text


def test_unknown_feed_name():
    reg = b.build_tool_registry(b.TOOLS_CONFIG, bsky=FakeBsky([{"handle": "a", "text": "x"}]),
                                router=FakeRouter())
    res = reg.execute("summarize_feed", {"feed_name": "no-existe"}, _ctx())
    assert "no conozco" in res.text


def test_missing_deps_is_graceful():
    # Sin bsky/router (ej. contexto de test que no los provee) → no crashea.
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    res = reg.execute("summarize_feed", {}, _ctx())
    assert "no disponible" in res.text


def test_toggle_disables_tool():
    cfg = dict(b.TOOLS_CONFIG)
    cfg["summarize_feed"] = {"enabled": False, "scopes": ["reply"]}
    reg = b.build_tool_registry(cfg, bsky=FakeBsky([]), router=FakeRouter())
    assert "summarize_feed" not in [t.name for t in reg.available(Scope.REPLY)]
