"""Tests del schema de eventos / calendario (T4, db.py). Usa una DB temporal real."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db as d  # noqa: E402

_AR = timezone(timedelta(hours=-3))


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "events_test.db")
    # segunda init: el schema es idempotente (CREATE TABLE IF NOT EXISTS)
    d.init_db(tmp_path / "events_test.db")
    c.execute("INSERT OR IGNORE INTO users(handle) VALUES ('ppolci.com')")
    c.commit()
    return c


def _day(offset: int) -> str:
    return (datetime.now(_AR) + timedelta(days=offset)).strftime("%Y-%m-%d")


def test_crud_roundtrip(conn):
    eid = d.create_event(
        conn, title="Cumple", event_at=f"{_day(0)}T20:00",
        handle="ppolci.com", kind="birthday", source="admin",
    )
    assert isinstance(eid, int)
    ev = d.get_event(conn, eid)
    assert ev["title"] == "Cumple" and ev["kind"] == "birthday"

    assert d.update_event(conn, eid, title="Cumpleaños", description="20hs") is True
    ev = d.get_event(conn, eid)
    assert ev["title"] == "Cumpleaños" and ev["description"] == "20hs"

    assert d.update_event(conn, eid, unknown_field="x") is True  # ignora claves desconocidas
    assert d.delete_event(conn, eid) is True
    assert d.get_event(conn, eid) is None
    assert d.delete_event(conn, eid) is False  # ya no existe


def test_upcoming_excludes_past(conn):
    d.create_event(conn, title="futuro", event_at=f"{_day(2)}T10:00")
    d.create_event(conn, title="pasado", event_at="2020-01-01T10:00")
    titles = [e["title"] for e in d.upcoming_events(conn, limit=10)]
    assert "futuro" in titles and "pasado" not in titles


def test_events_today_ar(conn):
    d.create_event(conn, title="hoy", event_at=f"{_day(0)}T12:00", handle="ppolci.com")
    d.create_event(conn, title="mañana", event_at=f"{_day(1)}T12:00")
    titles = [e["title"] for e in d.events_today(conn)]
    assert titles == ["hoy"]


def test_handle_filter_includes_community(conn):
    conn.execute("INSERT OR IGNORE INTO users(handle) VALUES ('otro.com')")
    conn.commit()
    d.create_event(conn, title="del user", event_at=f"{_day(1)}T10:00", handle="ppolci.com")
    d.create_event(conn, title="comunidad", event_at=f"{_day(1)}T11:00")  # handle NULL
    d.create_event(conn, title="ajeno", event_at=f"{_day(1)}T12:00", handle="otro.com")
    titles = {e["title"] for e in d.upcoming_events(conn, handle="ppolci.com")}
    assert titles == {"del user", "comunidad"}  # incluye comunidad, excluye ajeno


def test_fk_cascade_on_user_delete(conn):
    d.create_event(conn, title="del user", event_at=f"{_day(1)}T10:00", handle="ppolci.com")
    d.create_event(conn, title="comunidad", event_at=f"{_day(1)}T11:00")
    conn.execute("DELETE FROM users WHERE handle='ppolci.com'")
    conn.commit()
    remaining = [r["title"] for r in conn.execute("SELECT title FROM events").fetchall()]
    assert remaining == ["comunidad"]  # el del user cascadeó, el de comunidad quedó
