"""Tests de la tarea `calendar` (el calendario ACTÚA SIEMPRE): vencimientos,
elegibilidad por creador (CALENDAR_ANNOUNCE) y redacción con fallback."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b
import db as d


def _now_ar() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


@pytest.fixture
def conn(tmp_path):
    c = d.init_db(tmp_path / "botata.db")
    yield c
    c.close()


class FakeBsky:
    def __init__(self):
        self.posts = []

    def post(self, text, limit=295, media_path=None):
        self.posts.append(text)
        return f"at://post/{len(self.posts)}"


class FakeRouter:
    def __init__(self, text="¡hoy hay juntada, vengan!", fail=False):
        self.text, self.fail = text, fail

    def chat(self, role, messages, **kw):
        if self.fail:
            raise RuntimeError("LLM caído")
        return self.text


# ─── zona horaria de la instancia (settings TIMEZONE) ───────────────────────
def test_set_local_tz_offset_y_iana():
    old = d.LOCAL_TZ
    try:
        assert d.set_local_tz("UTC+5:30") == "UTC+5:30"
        assert d.LOCAL_TZ.utcoffset(None) == timedelta(hours=5, minutes=30)
        assert d.set_local_tz("America/Argentina/Buenos_Aires") \
            == "America/Argentina/Buenos_Aires"
        # inválida → warning y queda la vigente (no rompe el arranque)
        d.set_local_tz("Zona/Inexistente")
        assert str(d.LOCAL_TZ) == "America/Argentina/Buenos_Aires"
        # vacía → no-op
        d.set_local_tz("")
        assert str(d.LOCAL_TZ) == "America/Argentina/Buenos_Aires"
    finally:
        d.LOCAL_TZ = old


def test_local_now_usa_local_tz(monkeypatch):
    monkeypatch.setattr(d, "LOCAL_TZ", timezone(timedelta(hours=-3)))
    esperado = datetime.now(timezone.utc) - timedelta(hours=3)
    assert abs((d.local_now() - esperado.replace(tzinfo=None)).total_seconds()) < 5
    assert d.local_now().tzinfo is None  # naive, como event_at


# ─── due_calendar_announcements ──────────────────────────────────────────────
def test_due_puntual_vencido_y_no_anunciado(conn):
    d.create_event(conn, title="juntada", event_at="2026-07-25T21:00",
                   handle=None, kind="community", source="ui")
    due = d.due_calendar_announcements(conn, now="2026-07-25T21:03")
    assert [e["title"] for e in due] == ["juntada"]
    assert due[0]["occurrence"] == "2026-07-25T21:00"
    # todavía no vencido → nada
    assert d.due_calendar_announcements(conn, now="2026-07-25T20:59") == []


def test_due_respeta_anunciado_y_gracia(conn):
    eid = d.create_event(conn, title="juntada", event_at="2026-07-25T21:00",
                         handle=None, kind="community", source="ui")
    d.mark_event_announced(conn, eid, "2026-07-25T21:00")
    assert d.due_calendar_announcements(conn, now="2026-07-25T22:00") == []
    # fuera de la ventana de gracia (24h) tampoco se anuncia tarde
    d.create_event(conn, title="viejo", event_at="2026-07-20T10:00",
                   handle=None, kind="community", source="ui")
    assert d.due_calendar_announcements(conn, now="2026-07-25T22:00") == []


def test_due_recurrente_una_vez_por_ocurrencia(conn):
    eid = d.create_event(conn, title="ronda semanal", event_at="2026-07-01T20:00",
                         handle=None, kind="community", source="ui", recur="weekly")
    # 2026-07-01 fue miércoles → vence cada miércoles 20:00
    due = d.due_calendar_announcements(conn, now="2026-07-22T20:05")
    assert due and due[0]["occurrence"] == "2026-07-22T20:00"
    d.mark_event_announced(conn, eid, "2026-07-22T20:00")
    assert d.due_calendar_announcements(conn, now="2026-07-22T23:00") == []
    # la semana siguiente vuelve a vencer
    assert d.due_calendar_announcements(conn, now="2026-07-29T20:05") != []


def test_due_recurrente_yearly(conn):
    eid = d.create_event(conn, title="cumple ana", event_at="2000-08-15T09:00",
                         handle=None, kind="birthday", source="ui", recur="yearly")
    due = d.due_calendar_announcements(conn, now="2026-08-15T09:05")
    assert due and due[0]["occurrence"] == "2026-08-15T09:00"
    d.mark_event_announced(conn, eid, "2026-08-15T09:00")
    assert d.due_calendar_announcements(conn, now="2026-08-15T12:00") == []


def test_events_today_yearly(conn):
    d.create_event(conn, title="cumple", event_at="2000-08-15", handle=None,
                   kind="birthday", source="ui", recur="yearly")
    assert [e["title"] for e in d.events_today(conn, day="2026-08-15")] == ["cumple"]
    assert d.events_today(conn, day="2026-08-16") == []


def test_bot_actions_quedan_afuera(conn):
    d.create_event(conn, title="postear himno", event_at="2026-07-25T00:00",
                   handle=None, kind="bot_action", source="admin")
    assert d.due_calendar_announcements(conn, now="2026-07-25T01:00") == []


# ─── elegibilidad (CALENDAR_ANNOUNCE) ────────────────────────────────────────
def test_event_creator():
    assert b._event_creator("ui") == "admin"
    assert b._event_creator("admin") == "admin"
    assert b._event_creator("feed") == "feed"  # terceros: NO cuenta como admin
    assert b._event_creator("tool:@ana.test") == "ana.test"
    assert b._event_creator("") is None
    assert b._event_creator(None) is None


class FakeRegistry:
    def groups_for(self, handle):
        return ["power_users"] if handle == "ana.test" else []


def test_announce_eligible(monkeypatch):
    ev_admin = {"source": "ui"}
    ev_user = {"source": "tool:@ana.test"}
    ev_otro = {"source": "tool:@otro.test"}
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    assert b._announce_eligible(ev_admin, None)
    assert not b._announce_eligible(ev_user, None)
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE",
                        {"from": "groups", "groups": ["power_users"]})
    reg = FakeRegistry()
    assert b._announce_eligible(ev_admin, reg)
    assert b._announce_eligible(ev_user, reg)
    assert not b._announce_eligible(ev_otro, reg)
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "any"})
    assert b._announce_eligible(ev_otro, None)


def test_announce_feed_es_opt_in(monkeypatch):
    ev_feed = {"source": "feed"}
    # default cerrado, incluso con from=any (contenido de terceros)
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    assert not b._announce_eligible(ev_feed, None)
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "any"})
    assert not b._announce_eligible(ev_feed, None)
    # opt-in explícito
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin", "feed": True})
    assert b._announce_eligible(ev_feed, None)


# ─── run_calendar_pass ───────────────────────────────────────────────────────
def test_pass_anuncia_y_marca(conn, monkeypatch):
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    d.create_event(conn, title="juntada", event_at=_iso(_now_ar() - timedelta(minutes=5)),
                   handle=None, kind="community", source="ui")
    bsky = FakeBsky()
    b.run_calendar_pass(bsky, FakeRouter(), conn)
    assert bsky.posts == ["¡hoy hay juntada, vengan!"]
    # segunda corrida: ya anunciado, no repite
    b.run_calendar_pass(bsky, FakeRouter(), conn)
    assert len(bsky.posts) == 1


def test_pass_fallback_si_llm_falla(conn, monkeypatch):
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    d.create_event(conn, title="juntada", event_at=_iso(_now_ar() - timedelta(minutes=5)),
                   handle=None, kind="community", source="ui")
    bsky = FakeBsky()
    b.run_calendar_pass(bsky, FakeRouter(fail=True), conn)
    assert len(bsky.posts) == 1
    assert "juntada" in bsky.posts[0] and bsky.posts[0].startswith("📅")


def test_pass_no_elegible_marca_sin_postear(conn, monkeypatch):
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    conn.execute("INSERT INTO users(handle) VALUES ('ana.test')")
    d.create_event(conn, title="mi finde", event_at=_iso(_now_ar() - timedelta(minutes=5)),
                   handle="ana.test", kind="other", source="tool:@ana.test")
    bsky = FakeBsky()
    b.run_calendar_pass(bsky, FakeRouter(), conn)
    assert bsky.posts == []
    assert d.due_calendar_announcements(conn) == []  # marcado igual


# ─── switch por evento (events.announce) ─────────────────────────────────────
def test_default_announce_al_crear(monkeypatch):
    """El switch nace resuelto por la política + creador; 'groups' con no-admin
    difiere (None) porque la membresía puede requerir red."""
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    assert b._default_announce("ui") is True
    assert b._default_announce("tool:@ana.test") is False
    assert b._default_announce("feed") is False
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "any"})
    assert b._default_announce("tool:@ana.test") is True
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "groups", "groups": ["g"]})
    assert b._default_announce("tool:@ana.test") is None
    assert b._default_announce("ui") is True
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin", "feed": True})
    assert b._default_announce("feed") is True


def test_switch_apagado_marca_sin_postear(conn, monkeypatch):
    """announce=0 explícito manda aunque el creador sea admin: se marca sin post."""
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    eid = d.create_event(conn, title="secreto", event_at=_iso(_now_ar() - timedelta(minutes=5)),
                         handle=None, kind="community", source="ui", announce=False)
    bsky = FakeBsky()
    b.run_calendar_pass(bsky, FakeRouter(), conn)
    assert bsky.posts == []
    assert d.due_calendar_announcements(conn) == []  # marcado igual
    # y el switch se puede prender después (UI) para la próxima ocurrencia
    d.set_event_announce(conn, eid, True)
    assert conn.execute("SELECT announce FROM events WHERE id=?", (eid,)).fetchone()[0] == 1


def test_switch_prendido_anuncia_aunque_el_gate_diga_no(conn, monkeypatch):
    """announce=1 explícito (ej. import del admin o toggle en la UI) manda sobre
    la política: el evento de un usuario se anuncia igual."""
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    conn.execute("INSERT INTO users(handle) VALUES ('ana.test')")
    d.create_event(conn, title="juntada", event_at=_iso(_now_ar() - timedelta(minutes=5)),
                   handle="ana.test", kind="other", source="tool:@ana.test", announce=True)
    bsky = FakeBsky()
    b.run_calendar_pass(bsky, FakeRouter(), conn)
    assert len(bsky.posts) == 1


def test_switch_null_cae_al_gate_legado(conn, monkeypatch):
    """announce NULL (evento legado) → decide el gate CALENDAR_ANNOUNCE de siempre."""
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    conn.execute("INSERT INTO users(handle) VALUES ('ana.test')")
    d.create_event(conn, title="mi finde", event_at=_iso(_now_ar() - timedelta(minutes=5)),
                   handle="ana.test", kind="other", source="tool:@ana.test")  # announce=None
    bsky = FakeBsky()
    b.run_calendar_pass(bsky, FakeRouter(), conn)
    assert bsky.posts == []  # gate 'admin' lo declina


def test_pass_fallo_de_red_reintenta(conn, monkeypatch):
    monkeypatch.setattr(b, "CALENDAR_ANNOUNCE", {"from": "admin"})
    d.create_event(conn, title="juntada", event_at=_iso(_now_ar() - timedelta(minutes=5)),
                   handle=None, kind="community", source="ui")

    class BrokenBsky:
        def post(self, *a, **k):
            raise RuntimeError("red caída")

    b.run_calendar_pass(BrokenBsky(), FakeRouter(), conn)
    assert d.due_calendar_announcements(conn) != []  # sigue pendiente
    bsky = FakeBsky()
    b.run_calendar_pass(bsky, FakeRouter(), conn)
    assert len(bsky.posts) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
