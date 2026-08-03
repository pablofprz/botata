"""Tests de `like_post`: el bot marca "me gusta" en lo que le gusta.

El like contra la red va MOCKEADO (canal falso). Lo que se verifica acá es el
contrato de la tool: sobre qué post actúa (el que tiene delante, no uno que el
LLM elija), la idempotencia entre reintentos y que un canal que no puede likear
no rompe el flujo de respuesta.
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
from tools import Scope, ToolContext  # noqa: E402


class FakeCanal:
    def __init__(self, ok=True):
        self.ok = ok
        self.likes: list[tuple[str, str]] = []

    def like_post(self, uri, cid=""):
        self.likes.append((uri, cid))
        return self.ok


class CanalSinLike:
    """Un canal que no implementa el gesto (contrato viejo)."""


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "like.db")


def _state(uri="at://post/1", cid="cid1"):
    return {"mention_uri": uri, "mention_cid": cid, "author_handle": "ana"}


def test_likea_el_post_que_tiene_delante(conn):
    canal = FakeCanal()
    res = b._make_like_post_tool(canal)({}, ToolContext(state=_state(), conn=conn))
    assert canal.likes == [("at://post/1", "cid1")]
    assert "me gusta" in res.text


def test_no_duplica_el_like_en_un_reintento(conn):
    canal = FakeCanal()
    ctx = ToolContext(state=_state(), conn=conn)
    b._make_like_post_tool(canal)({}, ctx)
    res = b._make_like_post_tool(canal)({}, ctx)
    assert len(canal.likes) == 1                 # el segundo no llega a la red
    assert "ya le habías puesto" in res.text


def test_like_fallido_no_se_marca_como_hecho(conn):
    canal = FakeCanal(ok=False)
    ctx = ToolContext(state=_state(), conn=conn)
    res = b._make_like_post_tool(canal)({}, ctx)
    assert "no pude" in res.text
    assert d.kv_get(conn, "liked:at://post/1") is None
    canal.ok = True                              # el próximo intento sí puede
    b._make_like_post_tool(canal)({}, ctx)
    assert len(canal.likes) == 2


def test_canal_sin_me_gusta_no_rompe(conn):
    res = b._make_like_post_tool(CanalSinLike())({}, ToolContext(state=_state(), conn=conn))
    assert "no tiene me gusta" in res.text


def test_sin_post_no_hace_nada(conn):
    res = b._make_like_post_tool(FakeCanal())({}, ToolContext(state={}, conn=conn))
    assert "ningún post" in res.text


def test_la_tool_no_acepta_una_uri_del_llm(conn):
    """El objetivo NO es parametrizable: el LLM no puede apuntar a otro post."""
    canal = FakeCanal()
    b._make_like_post_tool(canal)({"uri": "at://otro/999"},
                                  ToolContext(state=_state(), conn=conn))
    assert canal.likes == [("at://post/1", "cid1")]
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert reg.get("like_post").parameters["properties"] == {}


def test_scopes_default():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "like_post" in [t.name for t in reg.available(Scope.REPLY)]
    # No en el pase de feed (solo lectura) ni como comando de admin.
    assert "like_post" not in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
    assert "like_post" not in [t.name for t in reg.available(Scope.ADMIN)]


def test_se_puede_apagar_por_config():
    reg = b.build_tool_registry({"like_post": {"enabled": False}})
    assert "like_post" not in [t.name for t in reg.available(Scope.REPLY)]
