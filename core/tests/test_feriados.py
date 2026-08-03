"""Navidad y Año nuevo: los dos eventos que existen en cualquier comunidad.

Se siembran solos al abrir la DB (una vez por instancia, guardado por kv), como
eventos ANUALES de comunidad. Lo que importa: que no se dupliquen en cada
arranque y que si el admin los borra no vuelvan.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as dbmod  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    return dbmod.init_db(tmp_path / "feriados.db")


def _titulos(conn) -> list[str]:
    return [r["title"] for r in conn.execute("SELECT title FROM events ORDER BY event_at")]


def test_se_siembran_una_vez(conn, monkeypatch):
    monkeypatch.setattr(b, "LANGUAGE", "es")
    b._sembrar_feriados(conn)
    assert _titulos(conn) == ["Año nuevo", "Navidad"]
    filas = conn.execute("SELECT recur, handle, announce FROM events").fetchall()
    assert all(f["recur"] == "yearly" for f in filas)      # se repiten solos
    assert all(f["handle"] is None for f in filas)         # son de la comunidad
    assert all(f["announce"] == 1 for f in filas)          # y se anuncian


def test_no_se_duplican_en_cada_arranque(conn, monkeypatch):
    monkeypatch.setattr(b, "LANGUAGE", "es")
    for _ in range(3):
        b._sembrar_feriados(conn)
    assert len(_titulos(conn)) == 2


def test_si_el_admin_los_borra_no_vuelven(conn, monkeypatch):
    """El guard es el kv, no "¿existe el evento?": el calendario es del admin."""
    monkeypatch.setattr(b, "LANGUAGE", "es")
    b._sembrar_feriados(conn)
    conn.execute("DELETE FROM events")
    conn.commit()
    b._sembrar_feriados(conn)
    assert _titulos(conn) == []


def test_respeta_el_idioma_de_la_instancia(conn, monkeypatch):
    monkeypatch.setattr(b, "LANGUAGE", "english")
    b._sembrar_feriados(conn)
    assert _titulos(conn) == ["New Year", "Christmas"]


def test_idioma_desconocido_cae_a_ingles(conn, monkeypatch):
    monkeypatch.setattr(b, "LANGUAGE", "portuñol rioplatense")
    b._sembrar_feriados(conn)
    assert _titulos(conn) == ["New Year", "Christmas"]


def test_aparecen_como_proximos_aunque_ya_pasaron_este_anio(conn, monkeypatch):
    """La primera ocurrencia queda en el año en curso: si hoy es julio, Navidad
    de este año sigue siendo futura y Año nuevo salta al que viene."""
    monkeypatch.setattr(b, "LANGUAGE", "es")
    b._sembrar_feriados(conn)
    proximos = [e["title"] for e in dbmod.upcoming_events(conn, limit=10)]
    assert "Navidad" in proximos and "Año nuevo" in proximos


def test_un_error_no_impide_arrancar(conn, monkeypatch):
    monkeypatch.setattr(b, "LANGUAGE", "es")
    monkeypatch.setattr(dbmod, "create_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db rota")))
    b._sembrar_feriados(conn)                              # no lanza
    assert dbmod.kv_get(conn, b._FERIADOS_KV) is None      # y lo reintenta después


# ─── fecha y hora en el contexto (por qué el bot se perdía en los días) ──────
def test_la_linea_de_fecha_trae_el_dia_de_la_semana(monkeypatch):
    """El día de la semana es lo primero que el bot usa para hablar ("lunes de
    Dio"), así que tiene que estar y tiene que ser el correcto."""
    from datetime import datetime, timedelta, timezone
    ar = timezone(timedelta(hours=-3))
    esperado = {  # 2026: 26/7 domingo … 1/8 sábado
        26: "domingo", 27: "lunes", 28: "martes", 29: "miércoles",
        30: "jueves", 31: "viernes",
    }
    for dia, nombre in esperado.items():
        monkeypatch.setattr(b, "now_local",
                            lambda d=dia: datetime(2026, 7, d, 15, 30, tzinfo=ar))
        linea = b.current_datetime_line()
        assert linea.startswith(f"Fecha y hora actual: {nombre} {dia} de julio de 2026, 15:30")


def test_los_timestamps_de_la_db_se_muestran_en_hora_local():
    """La DB guarda UTC (`datetime('now')`) y el bot vive en -3: leía su propia
    historia tres horas adelantada, y pasadas las 21:00 locales TODO lo que leía
    estaba fechado al día siguiente."""
    dbmod.set_local_tz("America/Argentina/Buenos_Aires")
    # domingo 22:30 local == lunes 01:30 UTC
    assert b.fecha_local("2026-07-27 01:30:00") == "2026-07-26"
    assert b.fecha_local("2026-07-27 01:30:00", con_hora=True) == "2026-07-26 22:30"


def test_una_fecha_sin_hora_no_se_corre_un_dia():
    """"2026-07-21" ya es una fecha, no un instante: convertirla la correría al
    20 (medianoche UTC = 21:00 del día anterior en -3)."""
    dbmod.set_local_tz("America/Argentina/Buenos_Aires")
    assert b.fecha_local("2026-07-21") == "2026-07-21"


def test_fecha_local_aguanta_basura():
    assert b.fecha_local(None) == "" and b.fecha_local("") == ""
    assert b.fecha_local("no es una fecha") == "no es una "
