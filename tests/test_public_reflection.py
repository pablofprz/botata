"""Tests de la reflexión pública (portada de maripobot: reflect_on_history).

El bot postea una reflexión en primera persona sobre lo que vivió. FakeLLM/
FakeBsky como en test_heartbeat; sin embeddings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "pubref.db")
    c.execute("INSERT INTO users(handle) VALUES ('u1.bsky.social')")
    c.commit()
    return c


class FakeBsky:
    def __init__(self):
        self.posts = []

    def post(self, text, image_path=None):
        self.posts.append(text)
        return f"at://fake/{len(self.posts)}"


class FakeLLM:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def complete(self, system, user, model_cls=None):
        self.calls += 1
        self.last_system = system
        self.last_user = user
        return self.decision


def _seed_activity(conn, n):
    for i in range(n):
        b.log_bot_post(conn, uri=f"at://bot/{i}", in_reply_to=None,
                       reply_to_handle="u1.bsky.social", text=f"respuesta {i}")


def _run(conn, bsky, llm, monkeypatch):
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_public_reflection_pass(bsky, router=None, conn=conn)


def test_sin_material_no_llama_llm(conn, monkeypatch):
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="pienso"))
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 0 and bsky.posts == []


def test_con_actividad_postea_reflexion(conn, monkeypatch):
    _seed_activity(conn, 5)
    decision = b.FeedDecision(should_post=True, reason="hubo tela",
                              text="aprendí que callar también es responder")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == ["aprendí que callar también es responder"]
    assert "respuesta 0" in llm.last_user            # la actividad llegó
    # la reflexión quedó registrada en bot_posts
    assert "aprendí que callar también es responder" in b.recent_bot_posts(conn, limit=10)


def test_lecciones_van_al_prompt(conn, monkeypatch):
    _seed_activity(conn, 5)
    conn.execute("INSERT INTO lessons(lesson_text, scope) VALUES ('variar los remates', 'community')")
    conn.commit()
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="nada"))
    _run(conn, bsky, llm, monkeypatch)
    assert "variar los remates" in llm.last_system


def test_solo_lecciones_sin_actividad_igual_corre(conn, monkeypatch):
    conn.execute("INSERT INTO lessons(lesson_text, scope) VALUES ('x', 'community')")
    conn.commit()
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="poco"))
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1


def test_should_post_false_no_postea(conn, monkeypatch):
    _seed_activity(conn, 5)
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="sin peso"))
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []


def test_dedup_contra_posts_recientes(conn, monkeypatch):
    _seed_activity(conn, 5)
    b.log_bot_post(conn, uri="at://viejo", in_reply_to=None, reply_to_handle=None,
                   text="ya reflexioné esto")
    bsky, llm = FakeBsky(), FakeLLM(
        b.FeedDecision(should_post=True, text="YA REFLEXIONÉ  esto"))
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []


def test_error_llm_no_rompe(conn, monkeypatch):
    _seed_activity(conn, 5)

    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(b, "RoleLLM", lambda router, role: BoomLLM())
    b.run_public_reflection_pass(FakeBsky(), router=None, conn=conn)  # no lanza
