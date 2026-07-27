"""Tests del pase de LECTURA del feed (T5/T6): fetch → learn → summarize.
Desde 2026-07-27 el pase es solo inward — NUNCA postea (la proactividad de
posteo vive en las rutinas). Mockea LLM y Bluesky; DB y grafo reales."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")  # botata lo lee a nivel módulo

import botata as b  # noqa: E402
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

    def post(self, text, limit=295, media_path=None):
        self.posted.append(text)
        return f"at://bot/post/{len(self.posted)}"


class FakeRouter:
    def chat(self, role, messages, **kw):
        return "Se habló del dólar y de una peli nueva."


class FakeRoleLLM:
    """Interfaz mínima de RoleLLM para el nodo learn (structured output)."""

    def __init__(self, learnings: dict | None = None):
        self._learnings = learnings or {}

    def complete(self, system, user, model):
        return model(**self._learnings)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = d.init_db(tmp_path / "feed_test.db")
    monkeypatch.setattr(b, "FEEDS_DIR", tmp_path)  # no tocar el context/feeds real
    bsky = FakeBsky()

    def make_graph(learnings: dict | None = None):
        monkeypatch.setattr(b, "RoleLLM", lambda router, role: FakeRoleLLM(learnings))
        return b.build_feed_graph(FakeRouter(), bsky, conn)

    return conn, bsky, make_graph


def _state(**over):
    base = {"feed_name": "polcifeed", "list_uri": "at://x", "interval_hours": 0,
            "full_backfill": False}
    base.update(over)
    return base


def test_lee_y_resume_sin_postear(env):
    conn, bsky, make_graph = env
    g = make_graph()
    res = g.invoke(_state())
    assert res.get("posts_count") == 2
    assert res.get("summary")            # el resumen se generó...
    assert bsky.posted == []             # ...y NO hubo ningún posteo
    assert (Path(b.FEEDS_DIR) / "polcifeed.md").exists()  # memoria del feed


def test_aprende_hechos_de_usuarios_conocidos(env):
    conn, bsky, make_graph = env
    conn.execute("INSERT OR IGNORE INTO users (handle) VALUES ('user1')")
    g = make_graph({"facts": [{"handle": "user1", "fact": "le interesa el dólar"}],
                    "events": []})
    res = g.invoke(_state())
    assert res.get("learned_facts") == 1
    assert bsky.posted == []


def test_learn_off_no_extrae(env):
    conn, bsky, make_graph = env
    conn.execute("INSERT OR IGNORE INTO users (handle) VALUES ('user1')")
    g = make_graph({"facts": [{"handle": "user1", "fact": "x"}], "events": []})
    res = g.invoke(_state(learn=False))
    assert not res.get("learned_facts")


def test_sin_posts_corta_temprano(env):
    conn, bsky, make_graph = env
    bsky.get_list_feed = lambda uri, since=None, limit=50: []
    g = make_graph()
    res = g.invoke(_state())
    assert res.get("posts_count") == 0 and res.get("summary") is None
