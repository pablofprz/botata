"""Tests de la tool `summarize_feed` (scope reply, on-demand) y su toggle por config."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
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


def test_registered_scopes():
    reg = b.build_tool_registry(b.TOOLS_CONFIG, bsky=FakeBsky([]), router=FakeRouter())
    assert "summarize_feed" in [t.name for t in reg.available(Scope.REPLY)]
    # 2026-07-27: también feed_reflection — las rutinas leen el clima del feed
    # antes de decidir qué postear ("leé los últimos posts y posteá acorde").
    assert "summarize_feed" in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
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


def test_hours_controla_la_ventana():
    """`hours` dice cuánta conversación leer hacia atrás (clamp 1–48, default 6)."""
    class SpyBsky(FakeBsky):
        def get_feed_posts(self, source_type, identifier, since=None, limit=50):
            self.since = since
            return self._posts

    from datetime import datetime, timezone
    posts = [{"handle": "a", "text": "hoy el dólar"}]
    for hours_arg, esperado in ((24, 24.0), (None, 6.0), (999, 48.0), (0, 1.0), ("banana", 6.0)):
        spy = SpyBsky(posts)
        reg = b.build_tool_registry(b.TOOLS_CONFIG, bsky=spy, router=FakeRouter())
        args = {} if hours_arg is None else {"hours": hours_arg}
        reg.execute("summarize_feed", args, _ctx())
        delta_h = (datetime.now(timezone.utc) - spy.since).total_seconds() / 3600
        assert abs(delta_h - esperado) < 0.1, f"hours={hours_arg}"


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
