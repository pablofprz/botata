"""Tests de eventos-acción (kind='bot_action'): órdenes agendadas para el bot.

Cubre: helpers de db (due/mark done), gating de permisos en create_event
(default solo admin, parametrizable), y ejecución + marca en el heartbeat.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import ToolContext  # noqa: E402

# Admin propio del test — no depender del settings desplegado (el del repo es neutro).
ADMIN = "admin.test"
U1 = "user1.bsky.social"
_AR = timezone(timedelta(hours=-3))


@pytest.fixture(autouse=True)
def _admin_de_test(monkeypatch):
    monkeypatch.setattr(b, "ADMIN_HANDLE", ADMIN)
    monkeypatch.setattr(b, "ADMIN_HANDLES", frozenset({ADMIN}))


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "actions.db")
    for h in (ADMIN, U1):
        c.execute("INSERT INTO users(handle) VALUES (?)", (h,))
    c.commit()
    return c


def _ctx(conn, author=None):
    state = {"author_handle": author} if author else {}
    return ToolContext(state=state, conn=conn)


def _hora(delta_h: int) -> str:
    return (datetime.now(_AR) + timedelta(hours=delta_h)).strftime("%Y-%m-%dT%H:%M")


# ─── db: due_bot_actions / mark_event_done ───────────────────────────────────
def test_due_solo_vencidas_y_no_done(conn):
    d.create_event(conn, title="vencida", event_at=_hora(-1), kind="bot_action")
    d.create_event(conn, title="futura", event_at=_hora(+2), kind="bot_action")
    d.create_event(conn, title="normal", event_at=_hora(-1), kind="reminder")
    due = d.due_bot_actions(conn)
    assert [e["title"] for e in due] == ["vencida"]


def test_mark_done_la_saca_de_due(conn):
    eid = d.create_event(conn, title="una", event_at=_hora(-1), kind="bot_action")
    assert len(d.due_bot_actions(conn)) == 1
    d.mark_event_done(conn, eid)
    assert d.due_bot_actions(conn) == []


# ─── tool create_event: permisos ─────────────────────────────────────────────
def test_admin_crea_bot_action_sin_duenio(conn):
    r = b._tool_create_event(
        {"title": "himno", "event_at": _hora(+1), "kind": "bot_action",
         "description": "postear el himno"}, _ctx(conn, ADMIN))
    assert "acción agendada" in r.text
    ev = conn.execute("SELECT * FROM events WHERE kind='bot_action'").fetchone()
    assert ev["handle"] is None          # la acción es del bot, no del pedidor
    assert ev["source"] == f"tool:@{ADMIN}"


def test_usuario_comun_degrada_a_reminder(conn):
    r = b._tool_create_event(
        {"title": "himno", "event_at": _hora(+1), "kind": "bot_action"}, _ctx(conn, U1))
    assert "solo puede el admin" in r.text
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='bot_action'").fetchone()[0] == 0
    ev = conn.execute("SELECT * FROM events WHERE kind='reminder'").fetchone()
    assert ev["handle"] == U1            # quedó como recordatorio personal


def test_config_any_habilita_usuarios(conn, monkeypatch):
    monkeypatch.setattr(b, "BOT_ACTIONS_FROM", "any")
    r = b._tool_create_event(
        {"title": "africa", "event_at": _hora(+1), "kind": "bot_action"}, _ctx(conn, U1))
    assert "acción agendada" in r.text
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='bot_action'").fetchone()[0] == 1


# ─── heartbeat: ejecuta y marca done ─────────────────────────────────────────
@pytest.fixture(autouse=True)
def _hb_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "HEARTBEAT_OVERRIDE_PATH", tmp_path / "heartbeat_override.md")
    monkeypatch.setattr(b, "HEARTBEAT_CHECKLIST_PATH", tmp_path / "heartbeat_checklist.md")


class FakeBsky:
    def __init__(self):
        self.posts = []

    def post(self, text, media_path=None):
        self.posts.append(text)
        return f"at://fake/{len(self.posts)}"


class FakeLLM:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def complete(self, system, user, model_cls=None):
        self.calls += 1
        self.last_system = system
        return self.decision


def _run_hb(conn, bsky, llm, monkeypatch):
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_heartbeat_pass(bsky, router=None, conn=conn)


def test_heartbeat_ejecuta_accion_vencida(conn, monkeypatch):
    eid = d.create_event(conn, title="himno", event_at=_hora(-1), kind="bot_action",
                         description="postear el himno completo")
    decision = b.FeedDecision(should_post=True, reason="orden", text="¡Oíd mortales!")
    bsky, llm = FakeBsky(), FakeLLM(decision)
    _run_hb(conn, bsky, llm, monkeypatch)
    assert bsky.posts == ["¡Oíd mortales!"]
    assert "ACCIONES AGENDADAS" in llm.last_system
    assert "postear el himno completo" in llm.last_system
    assert conn.execute("SELECT done FROM events WHERE id=?", (eid,)).fetchone()[0] == 1


def test_heartbeat_accion_declinada_igual_se_marca(conn, monkeypatch):
    """Una orden considerada no se reintenta para siempre: declinada → done."""
    eid = d.create_event(conn, title="x", event_at=_hora(-1), kind="bot_action")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=False, reason="ya lo dije"))
    _run_hb(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []
    assert conn.execute("SELECT done FROM events WHERE id=?", (eid,)).fetchone()[0] == 1


def test_heartbeat_accion_futura_no_dispara(conn, monkeypatch):
    d.create_event(conn, title="x", event_at=_hora(+3), kind="bot_action")
    bsky, llm = FakeBsky(), FakeLLM(b.FeedDecision(should_post=True, text="hola"))
    _run_hb(conn, bsky, llm, monkeypatch)
    # sin acciones vencidas, sin otros eventos ni instrucciones → ni llama al LLM
    # (la bot_action futura se filtra de hoy/próximos: es orden, no contexto)
    assert llm.calls == 0 and bsky.posts == []


def test_heartbeat_error_llm_no_marca(conn, monkeypatch):
    eid = d.create_event(conn, title="x", event_at=_hora(-1), kind="bot_action")

    class BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(b, "RoleLLM", lambda router, role: BoomLLM())
    b.run_heartbeat_pass(FakeBsky(), router=None, conn=conn)  # no lanza
    assert conn.execute("SELECT done FROM events WHERE id=?", (eid,)).fetchone()[0] == 0
