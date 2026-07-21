"""Tests de las tools de calendar (T9): get_upcoming_events / create_event.

Foco: regla de propiedad (usuario solo crea para sí; admin para comunidad/otros)
y privacidad del scoping (un usuario no ve eventos privados ajenos)."""
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

ADMIN = b.ADMIN_HANDLE
U1 = "user1.bsky.social"
U2 = "user2.bsky.social"


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "cal.db")
    for h in (ADMIN, U1, U2):
        c.execute("INSERT INTO users(handle) VALUES (?)", (h,))
    c.commit()
    return c


def _ctx(conn, author=None):
    state = {"author_handle": author} if author else {}
    return ToolContext(state=state, conn=conn)


def test_user_creates_only_for_self_ignores_handle(conn):
    # user1 intenta agendar "para" user2 → debe quedar como evento de user1.
    b._tool_create_event(
        {"title": "cumple", "event_at": "2026-08-01", "handle": U2}, _ctx(conn, U1))
    assert any(e["handle"] == U1 for e in d.upcoming_events(conn, handle=U1))
    # user2 no recibió nada.
    assert not any(e["handle"] == U2 for e in d.upcoming_events(conn, handle=None))


def test_admin_creates_community_event(conn):
    b._tool_create_event({"title": "juntada", "event_at": "2026-08-02"}, _ctx(conn, ADMIN))
    evs = d.upcoming_events(conn, handle=None)
    assert any(e["title"] == "juntada" and e["handle"] is None for e in evs)


def test_admin_creates_for_specific_user(conn):
    b._tool_create_event(
        {"title": "charla", "event_at": "2026-08-03", "handle": U2}, _ctx(conn, ADMIN))
    assert any(e["title"] == "charla" and e["handle"] == U2
               for e in d.upcoming_events(conn, handle=U2))


def test_create_event_is_idempotent(conn):
    b._tool_create_event({"title": "repetido", "event_at": "2026-08-04"}, _ctx(conn, ADMIN))
    r = b._tool_create_event({"title": "repetido", "event_at": "2026-08-04"}, _ctx(conn, ADMIN))
    assert "ya estaba agendado" in r.text
    assert len(d.upcoming_events(conn, handle=None)) == 1


def test_admin_cannot_target_user_without_profile(conn):
    r = b._tool_create_event(
        {"title": "x", "event_at": "2026-08-05", "handle": "nadie.bsky.social"}, _ctx(conn, ADMIN))
    assert "no tiene perfil" in r.text


def test_create_event_requires_title_and_date(conn):
    r = b._tool_create_event({"title": "", "event_at": ""}, _ctx(conn, ADMIN))
    assert "necesito" in r.text


def test_user_sees_own_and_community_not_others(conn):
    b._tool_create_event({"title": "comunidad-ev", "event_at": "2026-09-01"}, _ctx(conn, ADMIN))
    b._tool_create_event(
        {"title": "user2-ev", "event_at": "2026-09-02", "handle": U2}, _ctx(conn, ADMIN))
    b._tool_create_event({"title": "user1-ev", "event_at": "2026-09-03"}, _ctx(conn, U1))
    out = b._tool_get_upcoming_events({}, _ctx(conn, U1)).text
    assert "comunidad-ev" in out and "user1-ev" in out
    assert "user2-ev" not in out  # privacidad: no ve el evento privado de otro


def test_admin_and_proactive_loop_see_all(conn):
    b._tool_create_event(
        {"title": "user2-ev", "event_at": "2026-09-02", "handle": U2}, _ctx(conn, ADMIN))
    assert "user2-ev" in b._tool_get_upcoming_events({}, _ctx(conn, ADMIN)).text
    # loop proactivo: ctx sin author_handle (FeedState) → ve todo
    assert "user2-ev" in b._tool_get_upcoming_events({}, _ctx(conn)).text


def test_scopes_default(conn):
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "get_upcoming_events" in [t.name for t in reg.available(Scope.REPLY)]
    assert "get_upcoming_events" in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
    assert "create_event" in [t.name for t in reg.available(Scope.ADMIN)]
    # default actual: admin + usuarios (los usuarios agendan solo para sí — regla en el handler)
    assert "create_event" in [t.name for t in reg.available(Scope.REPLY)]
