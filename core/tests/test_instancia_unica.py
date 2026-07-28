"""Incidente 2026-07-27: dos bots sobre la misma instancia.

Quedó un bot huérfano de una prueba (la UI mataba el proceso lanzado pero no a
su hijo real, por el shim de pyenv) y otro lanzado desde la UI. Los dos
polleaban las mismas menciones y escribían la misma DB: uno marcaba la mención
`pending`, el otro la veía "ya atendida" y la salteaba. El bot dejó de
responder SIN UN SOLO ERROR en el log.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402


# ─── lock de instancia única ────────────────────────────────────────────────
@pytest.fixture()
def lock(tmp_path, monkeypatch):
    ruta = tmp_path / "bot.lock"
    monkeypatch.setattr(b, "_LOCK_PATH", ruta)
    yield ruta


def test_toma_el_lock_y_lo_suelta(lock):
    b.acquire_instance_lock()
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    b.release_instance_lock()
    assert not lock.exists()


def test_un_segundo_bot_no_arranca(lock):
    """El caso del incidente: el segundo proceso tiene que negarse, no convivir."""
    lock.write_text(json.dumps({"pid": os.getpid(), "desde": "hoy"}), encoding="utf-8")
    # simulamos "otro proceso vivo" con un PID distinto del nuestro
    import unittest.mock as mock
    with mock.patch.object(b, "_proceso_vivo", return_value=True):
        lock.write_text(json.dumps({"pid": os.getpid() + 1, "desde": "hoy"}),
                        encoding="utf-8")
        with pytest.raises(SystemExit) as e:
            b.acquire_instance_lock()
    assert "Ya hay un bot corriendo" in str(e.value)


def test_lock_huerfano_de_un_pid_muerto_se_toma(lock):
    lock.write_text(json.dumps({"pid": 999999999, "desde": "ayer"}), encoding="utf-8")
    b.acquire_instance_lock()                     # el PID no existe → se toma
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_el_lock_no_impide_arrancar_si_falla(lock, monkeypatch):
    """Un problema con el lock no puede ser motivo para que el bot no arranque."""
    monkeypatch.setattr(b, "_LOCK_PATH", Path("/carpeta/que/no/existe/bot.lock"))
    b.acquire_instance_lock()                     # no debe lanzar


def test_proceso_vivo_reconoce_el_propio():
    assert b._proceso_vivo(os.getpid()) is True
    assert b._proceso_vivo(999999999) is False
    assert b._proceso_vivo(0) is False


# ─── rescate de menciones colgadas en 'pending' ─────────────────────────────
@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "p.db")


def _mencion(conn, uri, status, hace_minutos):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=hace_minutos)).isoformat()
    conn.execute(
        "INSERT INTO replied_posts (uri, cid, author, status, replied_at, mode) "
        "VALUES (?,?,?,?,?,?)", (uri, "c", "ppolci.com", status, ts, "open"))
    conn.commit()


def test_rescata_el_pending_viejo(conn):
    _mencion(conn, "at://vieja", "pending", 30)
    assert b.has_replied(conn, "at://vieja") is True      # invisible: el bug
    b._rescatar_pending_colgados(conn)
    assert b.has_replied(conn, "at://vieja") is False     # vuelve a atenderse


def test_no_toca_el_pending_reciente(conn):
    """Uno recién marcado puede estar procesándose ahora mismo."""
    _mencion(conn, "at://ahora", "pending", 1)
    b._rescatar_pending_colgados(conn)
    assert b.has_replied(conn, "at://ahora") is True


def test_no_toca_los_ya_respondidos(conn):
    _mencion(conn, "at://ok", "replied", 60)
    _mencion(conn, "at://ign", "ignored", 60)
    b._rescatar_pending_colgados(conn)
    for uri in ("at://ok", "at://ign"):
        assert b.has_replied(conn, uri) is True
