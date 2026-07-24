"""Tests de la memoria de interacción (T-fix #5): tabla interactions +
UpdateProfileNode estructurado + inyección en el contexto del reply.

Spy sobre upsert_user_fact para no cargar bge-m3; log_interaction corre de
verdad (sin embeddings, barato).
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

U1 = "user1.bsky.social"


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "inter.db")
    c.execute("INSERT INTO users(handle) VALUES (?)", (U1,))
    c.commit()
    return c


# ─── db: log + recent ────────────────────────────────────────────────────────
def test_log_y_recent_cronologico(conn):
    for i in range(7):
        d.log_interaction(conn, U1, f"charla {i}", source_uri=f"at://{i}")
    recent = d.recent_interactions(conn, U1, limit=5)
    assert len(recent) == 5
    assert recent[0]["summary"] == "charla 6"   # más nueva primero
    assert recent[-1]["summary"] == "charla 2"


def test_purge_borra_interacciones(conn):
    d.log_interaction(conn, U1, "algo")
    counts = d.purge_user_memory(conn, U1)
    assert counts["interactions"] == 1
    assert d.recent_interactions(conn, U1) == []


def test_migrate_handle_mueve_interacciones(conn):
    d.log_interaction(conn, U1, "vieja charla")
    conn.execute("INSERT INTO users(handle) VALUES ('nuevo.bsky.social')")
    conn.commit()
    d.migrate_user_handle(conn, U1, "nuevo.bsky.social")
    assert d.recent_interactions(conn, "nuevo.bsky.social")[0]["summary"] == "vieja charla"
    assert d.recent_interactions(conn, U1) == []


# ─── UpdateProfileNode: structured output ────────────────────────────────────
class FakeLLM:
    def __init__(self, update: dict):
        self._update = update

    def complete(self, system, user, model_cls=None):
        return b.ProfileUpdate(**self._update)


def _state(text="hola bot"):
    return {"author_handle": U1, "mention_text": text, "mention_uri": "at://m/1",
            "thread_context": "", "reply_text": "hola!"}


@pytest.fixture()
def spy_facts(monkeypatch):
    calls: list[tuple[str, str]] = []

    def spy(c, handle, fact_text, source_uri=None, **kw):
        calls.append((handle, fact_text))
        return len(calls)

    monkeypatch.setattr(b.dbmod, "upsert_user_fact", spy)
    return calls


def test_nota_de_interaccion_siempre(conn, spy_facts):
    node = b.UpdateProfileNode(FakeLLM({"facts": [], "interaction_summary": "charla de fútbol"}), conn)
    node.run(_state())
    rec = d.recent_interactions(conn, U1)
    assert len(rec) == 1 and rec[0]["summary"] == "charla de fútbol"
    assert rec[0]["source_uri"] == "at://m/1"
    assert spy_facts == []  # sin facts forzados


def test_facts_y_nota_juntos(conn, spy_facts):
    node = b.UpdateProfileNode(FakeLLM({
        "facts": ["Vive en Rosario"], "interaction_summary": "me contó dónde vive"}), conn)
    node.run(_state())
    assert spy_facts == [(U1, "Vive en Rosario")]
    assert len(d.recent_interactions(conn, U1)) == 1


def test_summary_vacio_no_inserta(conn, spy_facts):
    node = b.UpdateProfileNode(FakeLLM({"facts": [], "interaction_summary": "  "}), conn)
    node.run(_state())
    assert d.recent_interactions(conn, U1) == []


def test_error_llm_no_rompe(conn, spy_facts):
    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("boom")

    node = b.UpdateProfileNode(BoomLLM(), conn)
    assert node.run(_state()) == {}
    assert d.recent_interactions(conn, U1) == []


# ─── GenerateReplyNode: inyección al contexto ────────────────────────────────
def test_reply_context_incluye_interacciones(conn, monkeypatch):
    d.log_interaction(conn, U1, "hablamos del mundial")
    captured = {}

    class CaptureLLM:
        def complete(self, system, user, model_cls=None):
            captured["system"] = system
            return b.BotReply(text="ok")

    monkeypatch.setattr(b.dbmod, "hybrid_search_user_facts", lambda *a, **k: [])
    monkeypatch.setattr(b.dbmod, "hybrid_search_lessons", lambda *a, **k: [])
    node = b.GenerateReplyNode(CaptureLLM(), conn, registry=None)
    out = node.run(_state("che bot"))
    assert out["reply_text"] == "ok"
    assert "hablamos del mundial" in captured["system"]
