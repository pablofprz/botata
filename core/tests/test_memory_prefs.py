"""Tests de la memoria general en DB (bot_memory, reemplaza context/MEMORY.md),
las preferencias (gustos/disgustos) y los bloques de contexto que se inyectan
en los prompts (memory_block / prefs_block / calendar_block)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import ToolContext  # noqa: E402


@pytest.fixture()
def conn():
    c = d.init_db(":memory:")
    yield c
    c.close()


def _ctx(conn, handle="user.test"):
    return ToolContext(state={"author_handle": handle}, conn=conn)


# ─── bot_memory (db) ──────────────────────────────────────────────────────────

def test_add_list_delete_memory(conn):
    mid = d.add_bot_memory(conn, "el bot es peronista", source="admin")
    assert mid is not None
    rows = d.list_bot_memory(conn)
    assert len(rows) == 1 and rows[0]["text"] == "el bot es peronista"
    assert d.delete_bot_memory(conn, mid)
    assert d.list_bot_memory(conn) == []


def test_memory_dedup_normalizado(conn):
    d.add_bot_memory(conn, "QUERÉS a panchitos")
    assert d.add_bot_memory(conn, "  querés a   panchitos ") is None
    assert len(d.list_bot_memory(conn)) == 1


def test_memory_created_at_explicito(conn):
    d.add_bot_memory(conn, "dato viejo", created_at="2026-07-21")
    assert d.list_bot_memory(conn)[0]["created_at"] == "2026-07-21"


def test_memory_block(conn):
    assert b.memory_block(conn) == ""
    d.add_bot_memory(conn, "sos fan de los panchos", created_at="2026-07-21")
    block = b.memory_block(conn)
    assert "Tu memoria general" in block and "panchos" in block and "2026-07-21" in block


# ─── migración de context/MEMORY.md ───────────────────────────────────────────

def test_migrate_memory_file(conn, tmp_path, monkeypatch):
    f = tmp_path / "MEMORY.md"
    f.write_text("# Memoria\n\n## 2026-07-21\n- QUERÉS a panchitos\n\n## 2026-07-22\n"
                 "- El bot es peronista\n- otra cosa\n", encoding="utf-8")
    monkeypatch.setattr(b, "MEMORY_PATH", f)
    b._migrate_memory_file(conn)
    rows = d.list_bot_memory(conn)
    assert [r["text"] for r in rows] == ["QUERÉS a panchitos", "El bot es peronista", "otra cosa"]
    assert rows[0]["created_at"] == "2026-07-21"
    assert all(r["source"] == "migration:MEMORY.md" for r in rows)
    # idempotente: el kv-guard evita re-ingestar
    b._migrate_memory_file(conn)
    assert len(d.list_bot_memory(conn)) == 3


def test_migrate_memory_file_ausente(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(b, "MEMORY_PATH", tmp_path / "no-existe.md")
    b._migrate_memory_file(conn)  # no explota
    assert d.list_bot_memory(conn) == []


# ─── tool save_to_memory (ahora a DB) ─────────────────────────────────────────

def test_save_to_memory_va_a_db(conn):
    res = b._tool_save_to_memory({"content": "la comunidad odia los lunes"}, _ctx(conn))
    assert "anotado" in res.text
    rows = d.list_bot_memory(conn)
    assert rows and rows[0]["text"] == "la comunidad odia los lunes"
    assert rows[0]["source"] == "tool:@user.test"
    res2 = b._tool_save_to_memory({"content": "la comunidad odia los lunes"}, _ctx(conn))
    assert "ya lo tenía" in res2.text


# ─── preferences (db) ─────────────────────────────────────────────────────────

def test_add_list_delete_preference(conn):
    pid = d.add_preference(conn, "like", "los panchos", source="admin")
    d.add_preference(conn, "dislike", "que me traten de usted")
    rows = d.list_preferences(conn)
    assert {r["kind"] for r in rows} == {"like", "dislike"}
    assert d.delete_preference(conn, pid)
    assert len(d.list_preferences(conn)) == 1


def test_preference_dedup_cross_kind(conn):
    d.add_preference(conn, "like", "el mate")
    assert d.add_preference(conn, "dislike", "El Mate") is None  # no puede ser ambos


def test_preference_kind_invalido(conn):
    with pytest.raises(ValueError):
        d.add_preference(conn, "meh", "x")


def test_find_preference(conn):
    d.add_preference(conn, "like", "Los Panchos")
    assert d.find_preference(conn, "los  panchos")["text"] == "Los Panchos"
    assert d.find_preference(conn, "nope") is None


def test_prefs_block(conn):
    assert b.prefs_block(conn) == ""
    d.add_preference(conn, "like", "los panchos")
    d.add_preference(conn, "dislike", "los lunes")
    block = b.prefs_block(conn)
    assert "Te gusta:" in block and "panchos" in block
    assert "No te gusta:" in block and "lunes" in block


# ─── tools de preferencias (modos) ────────────────────────────────────────────

@pytest.fixture()
def como_user(monkeypatch):
    monkeypatch.setattr(b, "ADMIN_HANDLES", frozenset({"admin.test"}))


def test_add_pref_manual_bloquea_al_bot(conn, como_user, monkeypatch):
    monkeypatch.setattr(b, "PREFS_MODE", "manual")
    res = b._tool_add_preference({"kind": "like", "text": "x"}, _ctx(conn))
    assert "modo manual" in res.text and d.list_preferences(conn) == []


def test_add_pref_add_only_permite_agregar(conn, como_user, monkeypatch):
    monkeypatch.setattr(b, "PREFS_MODE", "add_only")
    res = b._tool_add_preference({"kind": "like", "text": "el fernet"}, _ctx(conn))
    assert "anotado" in res.text
    assert d.list_preferences(conn)[0]["source"] == "bot"


def test_remove_pref_add_only_bloquea(conn, como_user, monkeypatch):
    monkeypatch.setattr(b, "PREFS_MODE", "add_only")
    d.add_preference(conn, "like", "el fernet", source="bot")
    res = b._tool_remove_preference({"text": "el fernet"}, _ctx(conn))
    assert "solo el admin" in res.text and len(d.list_preferences(conn)) == 1


def test_remove_pref_full_auto(conn, como_user, monkeypatch):
    monkeypatch.setattr(b, "PREFS_MODE", "full_auto")
    d.add_preference(conn, "like", "el fernet", source="bot")
    res = b._tool_remove_preference({"text": "el fernet"}, _ctx(conn))
    assert "saqué" in res.text and d.list_preferences(conn) == []


def test_remove_pref_full_auto_protege_admin(conn, como_user, monkeypatch):
    monkeypatch.setattr(b, "PREFS_MODE", "full_auto")
    d.add_preference(conn, "like", "los panchos", source="admin")
    res = b._tool_remove_preference({"text": "los panchos"}, _ctx(conn))
    assert "admin" in res.text and len(d.list_preferences(conn)) == 1


def test_admin_bypasea_modos(conn, como_user, monkeypatch):
    monkeypatch.setattr(b, "PREFS_MODE", "manual")
    ctx = _ctx(conn, handle="admin.test")
    assert "anotado" in b._tool_add_preference({"kind": "like", "text": "x"}, ctx).text
    assert d.list_preferences(conn)[0]["source"] == "admin"
    assert "saqué" in b._tool_remove_preference({"text": "x"}, ctx).text


# ─── calendar_block ───────────────────────────────────────────────────────────

def test_calendar_block(conn):
    assert b.calendar_block(conn) == ""
    d.create_event(conn, title="cumple de X", event_at="2099-01-01", kind="birthday")
    d.create_event(conn, title="orden secreta", event_at="2099-01-02", kind="bot_action")
    block = b.calendar_block(conn)
    assert "Calendario" in block and "cumple de X" in block
    assert "orden secreta" not in block  # bot_action excluido del contexto


# ─── facts de otros participantes del hilo ────────────────────────────────────

def test_other_participants_facts(conn, monkeypatch):
    node = b.GenerateReplyNode(llm=None, conn=conn)
    monkeypatch.setattr(b.dbmod, "user_exists", lambda c, h: h == "otro.test")
    monkeypatch.setattr(b.dbmod, "hybrid_search_user_facts",
                        lambda c, h, q, k=3: [(1, f"fact de {h}")])
    thread = "otro.test: hola\nautor.test: che bot\ndesconocido.test: jaja"
    out = node._other_participants_facts(thread, author="autor.test", query="q")
    assert "@otro.test" in out and "fact de otro.test" in out
    assert "desconocido.test" not in out  # sin perfil → no se busca


def test_other_participants_facts_sin_hilo(conn):
    node = b.GenerateReplyNode(llm=None, conn=conn)
    assert node._other_participants_facts("", author="a.test", query="q") == ""
