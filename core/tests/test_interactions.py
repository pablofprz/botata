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


# ─── MEMORY_SUSCEPTIBILITY: cuánto anota de cada charla ──────────────────────
# Nace de un dato medido (arg, 2026-08-03): 536 interacciones en una semana →
# 51 hechos. El prompt estricto es una decisión, y ahora es del admin.

def test_susceptibilidad_default_no_agrega_bloque():
    """La franja media (default 0.3) no repite lo que el prompt del archivo ya
    dice: cero costo de contexto para quien no tocó nada."""
    assert b._memory_hunger_block(b._memory_susceptibility()) == ""


def test_susceptibilidad_extremos(monkeypatch):
    assert "SOLO identidad dura" in b._memory_hunger_block(0.1)
    assert "generoso" in b._memory_hunger_block(0.6)
    assert "Ante la duda, anotalo" in b._memory_hunger_block(0.9)
    # valores rotos caen al default (franja media, sin bloque)
    monkeypatch.setattr(b, "settings", {**b.settings, "MEMORY_SUSCEPTIBILITY": "alto"})
    assert b._memory_susceptibility() == 0.3
    monkeypatch.setattr(b, "settings", {**b.settings, "MEMORY_SUSCEPTIBILITY": 7})
    assert b._memory_susceptibility() == 1.0  # clamp


def test_susceptibilidad_llega_al_prompt_del_nodo(conn, monkeypatch):
    monkeypatch.setattr(b, "settings", {**b.settings, "MEMORY_SUSCEPTIBILITY": 0.9})
    visto = {}

    class Espia:
        def complete(self, system, user, model_cls=None):
            visto["system"] = system
            return b.ProfileUpdate(facts=[], interaction_summary="x")

    b.UpdateProfileNode(Espia(), conn).run(_state())
    assert "Ante la duda, anotalo" in visto["system"]


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


def test_el_hecho_pedido_explicitamente_nace_fijado(conn, monkeypatch):
    """La distinción es INDEDUCIBLE después: una vez escrito, "acordate de que
    soy de Racing" y el bot anotándolo por su cuenta quedan idénticos. Por eso
    la marca la pone quien escribe, en el momento."""
    vistos: list[tuple[str, bool]] = []
    monkeypatch.setattr(b.dbmod, "upsert_user_fact",
                        lambda c, h, t, source_uri=None, pinned=False, **kw:
                        (vistos.append((t, pinned)), 1)[1])
    node = b.UpdateProfileNode(FakeLLM({"facts": [
        {"fact": "Es de Racing", "explicit": True},
        {"fact": "Ayer fue a la cancha", "explicit": False},
    ], "interaction_summary": "hablamos de fútbol"}), conn)
    node.run(_state())
    assert vistos == [("Es de Racing", True), ("Ayer fue a la cancha", False)]


def test_el_schema_acepta_el_hecho_como_texto_pelado():
    """El modelo devuelve tanto "un hecho" como {"fact": …}: son la misma
    respuesta, y absorberla sale más barato que pelearla."""
    p = b.ProfileUpdate(facts=["Vive en Rosario"], interaction_summary="x")
    assert p.facts[0].fact == "Vive en Rosario" and p.facts[0].explicit is False


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


def test_lo_fijado_entra_aunque_la_busqueda_no_traiga_nada(conn, monkeypatch):
    """El 📌 no puede depender de que la búsqueda lo encuentre: fijarlo es
    justamente lo que viene a evitar eso."""
    captured = {}

    class CaptureLLM:
        def complete(self, system, user, model_cls=None):
            captured["system"] = system
            return b.BotReply(text="ok")

    monkeypatch.setattr(b.dbmod, "pinned_user_facts",
                        lambda c, h, **k: [(1, "es de Racing, no de Boca")])
    monkeypatch.setattr(b.dbmod, "hybrid_search_user_facts", lambda *a, **k: [])
    monkeypatch.setattr(b.dbmod, "hybrid_search_lessons", lambda *a, **k: [])
    b.GenerateReplyNode(CaptureLLM(), conn, registry=None).run(_state("che bot"))
    assert "es de Racing, no de Boca" in captured["system"]
    # y se le avisa POR QUÉ está ahí, para que no lo trate como un dato más
    assert "te pidió que te acuerdes" in captured["system"]


def test_los_parametros_de_recuperacion_llegan_a_la_busqueda(conn, monkeypatch):
    """Los k estaban hardcodeados en 5; ahora salen de settings.RETRIEVAL."""
    pedidos = {}

    class CaptureLLM:
        def complete(self, system, user, model_cls=None):
            return b.BotReply(text="ok")

    monkeypatch.setattr(b, "K_USER_FACTS", 9)
    monkeypatch.setattr(b, "K_INTERACTIONS", 2)
    monkeypatch.setattr(b.dbmod, "pinned_user_facts", lambda c, h, **k: [])
    def _anotar(clave, valor):
        pedidos[clave] = valor
        return []
    monkeypatch.setattr(b.dbmod, "hybrid_search_user_facts",
                        lambda c, h, q, k=5: _anotar("facts", k))
    monkeypatch.setattr(b.dbmod, "hybrid_search_lessons", lambda *a, **k: [])
    monkeypatch.setattr(b.dbmod, "recent_interactions",
                        lambda c, h, limit=5: _anotar("inter", limit))
    b.GenerateReplyNode(CaptureLLM(), conn, registry=None).run(_state("che bot"))
    assert pedidos == {"facts": 9, "inter": 2}
