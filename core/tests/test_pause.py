"""Tests de /stop y /resume: pausa global explícita, solo admins, sin LLM."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b
import db as dbmod


class FakeGraph:
    def __init__(self):
        self.invocations = []

    def invoke(self, state):
        self.invocations.append(state["mention_uri"])


class FakeChannel:
    def __init__(self):
        self.replies = []

    def reply(self, text, parent_uri, parent_cid, root_uri, root_cid, media_path=None):
        self.replies.append({"text": text, "parent": parent_uri})
        return f"out://{len(self.replies)}"


def _mention(uri: str, author: str, text: str) -> dict:
    return {"uri": uri, "cid": uri, "author_handle": author, "text": text,
            "thread_context": "", "thread_root_uri": uri, "thread_root_cid": uri}


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn = dbmod.init_db(tmp_path / "botata.db")
    monkeypatch.setattr(b, "ADMIN_HANDLES", frozenset({"admin.test", "coadmin.test"}))
    yield conn, FakeGraph(), FakeChannel()
    conn.close()


def test_stop_pausa_y_confirma(env):
    conn, graph, ch = env
    b.process_mention(graph, conn, _mention("u1", "admin.test", "@bot.test /stop"),
                      "open", channel=ch)
    assert b.bot_paused(conn)
    assert graph.invocations == []                    # no pasó por el LLM
    assert "me pauso" in ch.replies[-1]["text"]
    assert b.has_replied(conn, "u1")                  # no se reprocesa


def test_pausado_encola_no_admins_y_atiende_admins(env):
    conn, graph, ch = env
    b.set_bot_paused(conn, True, by="admin.test")
    # no-admin: ni se procesa ni se marca (queda para después del /resume)
    b.process_mention(graph, conn, _mention("u2", "ana.test", "hola bot"),
                      "open", channel=ch)
    assert graph.invocations == [] and not b.has_replied(conn, "u2")
    # admin: sigue entrando al grafo (por acá viaja el /resume y la config)
    b.process_mention(graph, conn, _mention("u3", "coadmin.test", "cómo venís"),
                      "open", channel=ch)
    assert graph.invocations == ["u3"]


def test_resume_reanuda_y_responde_lo_encolado(env):
    conn, graph, ch = env
    b.set_bot_paused(conn, True, by="admin.test")
    b.process_mention(graph, conn, _mention("u4", "coadmin.test", "@bot.test /resume"),
                      "open", channel=ch)
    assert not b.bot_paused(conn)
    assert "de vuelta" in ch.replies[-1]["text"]
    # la mención que había quedado en cola ahora sí se procesa
    b.process_mention(graph, conn, _mention("u2", "ana.test", "hola bot"),
                      "open", channel=ch)
    assert graph.invocations == ["u2"]


def test_no_admin_no_puede_pausar(env):
    conn, graph, ch = env
    b.process_mention(graph, conn, _mention("u5", "ana.test", "@bot.test /stop"),
                      "open", channel=ch)
    assert not b.bot_paused(conn)
    assert graph.invocations == ["u5"]                # sigue el flujo normal


def test_comando_debe_ser_exacto(env):
    conn, graph, ch = env
    # texto extra → NO es el comando explícito: va al flujo normal
    b.process_mention(graph, conn, _mention("u6", "admin.test", "@bot.test /stop por favor"),
                      "open", channel=ch)
    assert not b.bot_paused(conn)
    assert graph.invocations == ["u6"]


def test_fallo_del_reply_no_impide_la_pausa(env):
    conn, graph, _ = env

    class BrokenChannel:
        def reply(self, *a, **k):
            raise RuntimeError("red caída")

    b.process_mention(graph, conn, _mention("u7", "admin.test", "/stop"),
                      "open", channel=BrokenChannel())
    assert b.bot_paused(conn)                         # el estado cambió igual


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
