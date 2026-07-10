"""Tests del loop proactivo de feed (T5). Mockea LLM y Bluesky; DB y grafo reales."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")  # butterbot lo lee a nivel módulo

import butterbot as b  # noqa: E402
import db as d  # noqa: E402


class FakeBsky:
    def __init__(self):
        self.posted: list[str] = []

    def get_list_feed(self, uri, since=None, limit=50):
        return [
            {"handle": "user1", "text": "hoy hubo debate sobre el dólar"},
            {"handle": "user2", "text": "alguien vio la peli nueva?"},
        ]

    def get_feed_posts(self, source_type, identifier, since=None, limit=50):
        return self.get_list_feed(identifier, since, limit)

    def post(self, text, limit=295, image_path=None):
        self.posted.append(text)
        return f"at://bot/post/{len(self.posted)}"


class FakeRouter:
    def chat(self, role, messages, **kw):
        return "Se habló del dólar y de una peli nueva."


class FakeRoleLLM:
    """Interfaz mínima de RoleLLM: no llama tools, devuelve una decisión fija."""

    def __init__(self, decision: dict):
        self._decision = decision

    def call_with_tools(self, system, user, tools):
        return (None, [])

    def complete(self, system, user, model):
        return model(**self._decision)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = d.init_db(tmp_path / "feed_test.db")
    monkeypatch.setattr(b, "FEEDS_DIR", tmp_path)  # no tocar el context/feeds real
    bsky = FakeBsky()
    reg = b.build_tool_registry(b.TOOLS_CONFIG)

    def make_graph(decision: dict):
        monkeypatch.setattr(b, "RoleLLM", lambda router, role: FakeRoleLLM(decision))
        return b.build_feed_graph(FakeRouter(), bsky, conn, reg)

    return conn, bsky, make_graph


def _state(**over):
    base = {
        "feed_name": "polcifeed", "list_uri": "at://x", "interval_hours": 0,
        "posting_policy": "balanced", "force_post": False, "full_backfill": False,
    }
    base.update(over)
    return base


def test_agent_decides_to_post(env):
    _, bsky, make_graph = env
    g = make_graph({"should_post": True, "reason": "tema del día", "text": "El dólar fue EL tema, ¿no?"})
    res = g.invoke(_state())
    assert res.get("posted_uri") and len(bsky.posted) == 1


def test_dedup_skips_repeat(env):
    _, bsky, make_graph = env
    g = make_graph({"should_post": True, "reason": "x", "text": "texto repetido"})
    g.invoke(_state())
    assert len(bsky.posted) == 1
    g.invoke(_state())  # mismo texto → duplicado
    assert len(bsky.posted) == 1  # no reposteó


def test_agent_abstains(env):
    _, bsky, make_graph = env
    g = make_graph({"should_post": False, "reason": "feed flojo", "text": ""})
    res = g.invoke(_state())
    assert not res.get("posted_uri") and bsky.posted == []


def test_force_post_overrides_abstention(env):
    _, bsky, make_graph = env
    g = make_graph({"should_post": False, "reason": "flojo", "text": "posteo forzado"})
    res = g.invoke(_state(force_post=True))
    assert res.get("posted_uri") and bsky.posted == ["posteo forzado"]
