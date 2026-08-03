"""Tests de `/stop` y `/start`: soltar un hilo.

Cualquiera puede pedirle al bot que suelte la conversación en la que está. Es un
comando LITERAL a propósito: la alternativa —leer "ya está, basta" con el LLM—
haría que el bot abandone hilos por su cuenta, que es justo lo que hay que
evitar. Todo esto corre ANTES del grafo: sin LLM, sin red.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as dbmod  # noqa: E402


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


def _mention(uri: str, author: str, text: str, root: str = "raiz") -> dict:
    return {"uri": uri, "cid": uri, "author_handle": author, "text": text,
            "thread_context": "", "thread_root_uri": root, "thread_root_cid": root}


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn = dbmod.init_db(tmp_path / "botata.db")
    monkeypatch.setattr(b, "ADMIN_HANDLE", "admin.test")
    monkeypatch.setattr(b, "ADMIN_HANDLES", frozenset({"admin.test"}))
    yield conn, FakeGraph(), FakeChannel()
    conn.close()


def test_stop_suelta_el_hilo_y_avisa(env):
    conn, graph, ch = env
    b.process_mention(graph, conn, _mention("u1", "ana.test", "@bot /stop"),
                      "open", channel=ch)
    assert b.hilo_mudo(conn, "raiz")
    assert graph.invocations == []                     # no pasó por el LLM
    assert "los dejo tranquilos" in ch.replies[-1]["text"]
    assert "/start" in ch.replies[-1]["text"]          # dice cómo volver
    assert b.has_replied(conn, "u1")


def test_en_un_hilo_soltado_no_contesta_a_nadie(env):
    conn, graph, ch = env
    b.set_hilo_mudo(conn, "raiz", True, by="ana.test")
    b.process_mention(graph, conn, _mention("u2", "ana.test", "che bot"),
                      "open", channel=ch)
    b.process_mention(graph, conn, _mention("u3", "otro.test", "@bot mirá esto"),
                      "open", channel=ch)
    # tampoco al admin: soltar es soltar (para eso está /start)
    b.process_mention(graph, conn, _mention("u4", "admin.test", "hola"),
                      "open", channel=ch)
    assert graph.invocations == [] and ch.replies == []
    # marcadas como ignoradas: no se reprocesan en cada poll
    assert all(b.has_replied(conn, u) for u in ("u2", "u3", "u4"))


def test_solo_afecta_a_ese_hilo(env):
    conn, graph, ch = env
    b.set_hilo_mudo(conn, "raiz", True, by="ana.test")
    b.process_mention(graph, conn, _mention("u5", "ana.test", "hola", root="otra-raiz"),
                      "open", channel=ch)
    assert graph.invocations == ["u5"]


def test_start_lo_trae_de_vuelta(env):
    conn, graph, ch = env
    b.set_hilo_mudo(conn, "raiz", True, by="ana.test")
    b.process_mention(graph, conn, _mention("u6", "otro.test", "@bot /start"),
                      "open", channel=ch)
    assert not b.hilo_mudo(conn, "raiz")
    assert "acá estoy" in ch.replies[-1]["text"]
    b.process_mention(graph, conn, _mention("u7", "ana.test", "volviste?"),
                      "open", channel=ch)
    assert graph.invocations == ["u7"]


def test_el_comando_tiene_que_ser_exacto(env):
    """"…y ahí /stop, no?" NO suelta el hilo: si no, el bot se iría de
    conversaciones por una frase que solo lo mencionaba."""
    conn, graph, ch = env
    for i, texto in enumerate(["@bot /stop por favor", "decile /stop a los demás",
                               "stop", "/stopear"]):
        b.process_mention(graph, conn, _mention(f"x{i}", "ana.test", texto),
                          "open", channel=ch)
    assert not b.hilo_mudo(conn, "raiz")
    assert len(graph.invocations) == 4                 # todas al flujo normal


def test_al_admin_se_le_aclara_que_no_lo_duerme(env):
    """El comando se llamaba /stop para dormir al bot entero: si el admin lo
    manda por costumbre, la respuesta le dice qué pasó y cuál era el otro."""
    conn, graph, ch = env
    b.process_mention(graph, conn, _mention("u8", "admin.test", "@bot /stop"),
                      "open", channel=ch)
    assert "/sleep" in ch.replies[-1]["text"]
    assert not b.bot_paused(conn)                      # NO se durmió entero


def test_soltar_un_hilo_no_duerme_al_bot(env):
    conn, graph, ch = env
    b.process_mention(graph, conn, _mention("u9", "ana.test", "/stop"),
                      "open", channel=ch)
    assert not b.bot_paused(conn)
    # y el bot sigue contestando en cualquier otro hilo
    b.process_mention(graph, conn, _mention("u10", "ana.test", "hola", root="otra"),
                      "open", channel=ch)
    assert graph.invocations == ["u10"]


def test_el_fallo_del_aviso_no_impide_soltar(env):
    conn, graph, _ = env

    class BrokenChannel:
        def reply(self, *a, **k):
            raise RuntimeError("red caída")

    b.process_mention(graph, conn, _mention("u11", "ana.test", "/stop"),
                      "open", channel=BrokenChannel())
    assert b.hilo_mudo(conn, "raiz")                   # el estado cambió igual


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
