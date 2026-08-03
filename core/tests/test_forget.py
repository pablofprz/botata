"""Olvidar UN dato (`forget_about_me`), no toda la memoria.

Antes la memoria de una persona era todo o nada: un dato mal anotado solo se
podía arreglar con `reset_my_memory`, perdiendo también lo que estaba bien.

Lo que se verifica acá es sobre todo lo que NO tiene que pasar: borrar de más,
borrar el hecho de otra persona, o borrar algo al azar cuando no encontró lo que
le pidieron. Es una tool destructiva que gatilla el LLM leyendo una frase.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

DIM = 1024
ANA, OTRO = "ana.test", "otro.test"


def _emb(v: float = 0.1) -> bytes:
    return struct.pack(f"<{DIM}f", *([v] * DIM))


def _fact(conn, handle, texto, *, pinned=False):
    conn.execute("INSERT OR IGNORE INTO users(handle) VALUES (?)", (handle,))
    conn.execute("INSERT INTO user_facts(handle, fact_text, pinned) VALUES (?, ?, ?)",
                 (handle, texto, 1 if pinned else 0))
    fid = conn.execute("SELECT id FROM user_facts ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO user_facts_vec(rowid, embedding, partition_key) "
                 "VALUES (?, ?, ?)", (fid, _emb(), handle))
    conn.commit()
    return fid


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "forget.db")


def _pedir(conn, que, handle=ANA):
    return b._tool_forget_about_me({"what": que},
                                   ToolContext(state={"author_handle": handle}, conn=conn))


def _textos(conn, handle=ANA):
    return [r["fact_text"] for r in conn.execute(
        "SELECT fact_text FROM user_facts WHERE handle = ? ORDER BY id", (handle,))]


def test_olvida_solo_el_dato_pedido(conn):
    _fact(conn, ANA, "vive en Rosario")
    _fact(conn, ANA, "es fan de los redondos")
    res = _pedir(conn, "que vivo en Rosario")
    assert "Rosario" in res.text
    assert _textos(conn) == ["es fan de los redondos"]


def test_borra_tambien_el_embedding(conn):
    """Un vector huérfano sigue ganando búsquedas con un rowid que ya no existe."""
    fid = _fact(conn, ANA, "vive en Rosario")
    _pedir(conn, "Rosario")
    assert conn.execute("SELECT COUNT(*) FROM user_facts_vec WHERE rowid = ?",
                        (fid,)).fetchone()[0] == 0


def test_puede_olvidar_un_hecho_fijado(conn):
    """Los 📌 están fuera de la búsqueda normal, pero si alguien pidió recordar
    algo también tiene derecho a que se lo olviden."""
    _fact(conn, ANA, "es celíaca", pinned=True)
    res = _pedir(conn, "que soy celíaca")
    assert "celíaca" in res.text and "recordar" in res.text
    assert _textos(conn) == []


def test_si_no_encuentra_nada_no_borra_nada(conn):
    """Sin una palabra en común no se adivina: es destructivo e irreversible."""
    _fact(conn, ANA, "vive en Rosario")
    _fact(conn, ANA, "es fan de los redondos")
    res = _pedir(conn, "mi color favorito")
    assert "no encontré nada" in res.text
    assert len(_textos(conn)) == 2


def test_nunca_toca_la_memoria_de_otro(conn):
    _fact(conn, OTRO, "vive en Rosario")
    _fact(conn, ANA, "vive en Córdoba")
    _pedir(conn, "que vivo en Rosario")          # el de Ana no matchea Rosario
    assert _textos(conn, OTRO) == ["vive en Rosario"]


def test_el_id_de_otra_persona_no_se_borra_aunque_se_cuele(conn):
    """Defensa en profundidad: el DELETE va scopeado por handle."""
    ajeno = _fact(conn, OTRO, "secreto ajeno")
    assert d.delete_user_fact(conn, ajeno, ANA) is None
    assert _textos(conn, OTRO) == ["secreto ajeno"]


def test_sin_texto_pregunta_en_vez_de_borrar(conn):
    _fact(conn, ANA, "vive en Rosario")
    assert "qué querés" in _pedir(conn, "  ").text
    assert len(_textos(conn)) == 1


def test_sin_memoria_lo_dice(conn):
    assert "no tengo nada guardado" in _pedir(conn, "cualquier cosa").text


def test_sin_autor_no_hace_nada(conn):
    _fact(conn, ANA, "vive en Rosario")
    res = b._tool_forget_about_me({"what": "Rosario"}, ToolContext(state={}, conn=conn))
    assert "no pude identificar" in res.text
    assert len(_textos(conn)) == 1


def test_scopes(conn):
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "forget_about_me" in [t.name for t in reg.available(Scope.REPLY)]
    # el pase proactivo no tiene a quién olvidar: no hay autor
    assert "forget_about_me" not in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
