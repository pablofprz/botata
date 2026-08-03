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

# Admin propio del test — no depender del settings desplegado (el del repo es neutro).
ADMIN = "admin.test"
U1 = "user1.bsky.social"
U2 = "user2.bsky.social"


@pytest.fixture(autouse=True)
def _admin_de_test(monkeypatch):
    monkeypatch.setattr(b, "ADMIN_HANDLE", ADMIN)
    monkeypatch.setattr(b, "ADMIN_HANDLES", frozenset({ADMIN}))


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


def test_everyone_sees_all_events(conn):
    # No hay eventos privados: el bot es de comunidades. `handle` dice de quién
    # es el evento (a quién saludar), no quién puede verlo.
    b._tool_create_event({"title": "comunidad-ev", "event_at": "2026-09-01"}, _ctx(conn, ADMIN))
    b._tool_create_event(
        {"title": "user2-ev", "event_at": "2026-09-02", "handle": U2}, _ctx(conn, ADMIN))
    b._tool_create_event({"title": "user1-ev", "event_at": "2026-09-03"}, _ctx(conn, U1))
    out = b._tool_get_upcoming_events({}, _ctx(conn, U1)).text
    assert "comunidad-ev" in out and "user1-ev" in out
    assert "user2-ev" in out  # ve también el cumple/evento de otro


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


# ─── /schedule y /agenda: el atajo al calendario ─────────────────────────────
# `/remember` es la memoria; `/schedule` es el calendario. La distinción la hace
# el classify_prompt, pero lo que garantiza que un usuario común pueda agendar es
# el RUTEO: un comando de no-admin cae al flujo de reply, que es donde vive
# create_event con scope reply.
def _clasificacion(**kw):
    base = dict(is_admin_command=False, command=None, skip=False,
                is_block_query=False, is_role_query=False)
    base.update(kw)
    return b.MentionClassification(**base)


def test_schedule_de_un_usuario_va_al_flujo_de_reply():
    estado = {"classification": _clasificacion(is_admin_command=True, command="schedule"),
              "is_admin": False}
    assert b.route_after_classify(estado) == "load_context"


def test_schedule_del_admin_va_al_nodo_de_admin():
    estado = {"classification": _clasificacion(is_admin_command=True, command="schedule"),
              "is_admin": True}
    assert b.route_after_classify(estado) == "handle_admin_command"


def test_las_tools_del_calendario_estan_abiertas_a_los_usuarios():
    """Sin scope reply, /schedule y /agenda solo funcionarían para el admin."""
    from tools import Scope
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    disponibles = [t.name for t in reg.available(Scope.REPLY)]
    assert "create_event" in disponibles          # /schedule
    assert "get_upcoming_events" in disponibles   # /agenda


def test_el_help_distingue_calendario_de_memoria():
    from tools import ToolContext
    texto = b._tool_get_help({}, ToolContext(state={}, conn=None)).text
    assert "/schedule" in texto and "/remember" in texto
    assert "calendario" in texto and "memoria" in texto
