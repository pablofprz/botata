"""Tests del pase heartbeat (T27): decide sobre eventos, postea o calla."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import db as d
import butterbot as b

_AR = timezone(timedelta(hours=-3))


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "hb_test.db")
    c.execute("INSERT OR IGNORE INTO users(handle) VALUES ('ppolci.com')")
    c.commit()
    return c


class FakeBsky:
    def __init__(self):
        self.posts = []

    def post(self, text, image_path=None):
        self.posts.append(text)
        return f"at://fake/{len(self.posts)}"


class FakeLLM:
    """Reemplaza RoleLLM: devuelve una decisión fija y cuenta llamadas."""

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def complete(self, system, user, model_cls=None):
        self.calls += 1
        self.last_system = system
        return self.decision


def _hoy_evento(conn, title="Cumple de Polci", handle="ppolci.com"):
    hoy = datetime.now(_AR).strftime("%Y-%m-%d")
    return d.create_event(conn, title=title, event_at=f"{hoy}T20:00",
                          handle=handle, kind="birthday", source="test")


def _run(conn, bsky, llm, monkeypatch):
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_heartbeat_pass(bsky, router=None, conn=conn)


def test_sin_eventos_no_llama_llm(conn, monkeypatch):
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="hola"))
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 0 and bsky.posts == []


def test_con_evento_postea(conn, monkeypatch):
    _hoy_evento(conn)
    decision = b.FeedDecision(should_post=True, reason="cumple", text="¡Feliz cumple @ppolci.com!")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1
    assert bsky.posts == ["¡Feliz cumple @ppolci.com!"]
    assert "Cumple de Polci" in llm.last_system          # el evento llegó al prompt
    assert b.recent_bot_posts(conn) == ["¡Feliz cumple @ppolci.com!"]  # registrado


def test_should_post_false_no_postea(conn, monkeypatch):
    _hoy_evento(conn)
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="nada nuevo"))
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 1 and bsky.posts == []


def test_dedup_contra_bot_posts(conn, monkeypatch):
    _hoy_evento(conn)
    b.log_bot_post(conn, uri="at://viejo", in_reply_to=None, reply_to_handle=None,
                   text="¡Feliz cumple @ppolci.com!")
    decision = b.FeedDecision(should_post=True, text="¡FELIZ CUMPLE  @ppolci.com!")  # ≈ igual
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []                              # normalizado → duplicado → skip


def test_texto_vacio_no_postea(conn, monkeypatch):
    _hoy_evento(conn)
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="  "))
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []


def test_error_del_llm_es_graceful(conn, monkeypatch):
    _hoy_evento(conn)

    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("api caída")

    bsky = FakeBsky()
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: BoomLLM())
    b.run_heartbeat_pass(bsky, router=None, conn=conn)   # no lanza
    assert bsky.posts == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
