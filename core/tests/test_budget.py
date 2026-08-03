"""Tests del guard de presupuesto diario (budget.py, portado de maripobot).

Fetcher y reloj inyectados: sin red, sin dormir. Estado en la tabla kv real
(db.init_db sobre tmp_path, barato — sin embeddings).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db as d
from budget import BudgetGuard


class FakeFetch:
    """Devuelve valores de una secuencia; el último se repite. None = red caída."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        v = self.values[0] if len(self.values) == 1 else self.values.pop(0)
        return v


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "budget.db")


def make_guard(conn, fetch, *, daily=1.0, enabled=True, day="2026-07-19"):
    days = {"value": day}
    g = BudgetGuard(
        get=lambda k: d.kv_get(conn, k),
        set=lambda k, v: d.kv_set(conn, k, v),
        fetch=fetch,
        daily_usd=daily,
        enabled=enabled,
        check_interval_s=0,  # sin cache: cada check lee fresco (determinista)
        now_day=lambda: days["value"],
    )
    return g, days


def test_disabled_nunca_quema_ni_toca_red(conn):
    fetch = FakeFetch(99.0)
    g, _ = make_guard(conn, fetch, enabled=False)
    assert g.check() is None and g.burned is False
    assert fetch.calls == 0


def test_snapshot_y_gasto_bajo_no_quema(conn):
    g, _ = make_guard(conn, FakeFetch(10.0, 10.5), daily=1.0)
    assert g.check() is None          # snapshot=10.0, gasto 0 → activo
    assert g.check() is None          # gasto 0.5 < 1.0
    assert g.burned is False


def test_quema_y_anuncia_una_sola_vez(conn):
    g, _ = make_guard(conn, FakeFetch(10.0, 11.5, 12.0), daily=1.0)
    g.check()                         # snapshot
    assert g.check() == "sleep"       # gasto 1.5 ≥ 1.0 → transición
    assert g.burned is True
    assert g.check() is None          # sigue quemado, sin re-anunciar
    assert g.burned is True


def test_dia_nuevo_despierta_y_anuncia(conn):
    g, days = make_guard(conn, FakeFetch(10.0, 11.5, 20.0, 20.1), daily=1.0)
    g.check()
    assert g.check() == "sleep"
    days["value"] = "2026-07-20"      # rollover: snapshot nuevo = 20.0
    assert g.check() == "wake"        # gasto 0.1 < 1.0 → despierta
    assert g.burned is False


def test_red_caida_es_pegajoso(conn):
    # Quemado + endpoint caído → sigue quemado (no despierta por un hiccup).
    g, _ = make_guard(conn, FakeFetch(10.0, 11.5, None, None), daily=1.0)
    g.check()
    assert g.check() == "sleep"
    assert g.check() is None and g.burned is True    # None → mantiene sleeping


def test_red_caida_activo_sigue_activo(conn):
    g, _ = make_guard(conn, FakeFetch(10.0, None), daily=1.0)
    g.check()                         # snapshot ok
    assert g.check() is None and g.burned is False


def test_snapshot_pendiente_reintenta(conn):
    # /credits caído al rollover → snapshot None; se reintenta hasta lograrlo.
    g, _ = make_guard(conn, FakeFetch(None, None, 10.0, 10.2), daily=1.0)
    assert g.check() is None and g.burned is False   # sin snapshot: fail-open
    assert g.check() is None                          # sigue sin snapshot
    assert g.check() is None                          # snapshot=10.0
    assert g.check() is None and g.burned is False    # gasto 0.2 < 1.0


def test_clamp_contra_refunds(conn):
    # total_usage "baja" (corrección del provider) → gasto clamp a 0, no negativo.
    g, _ = make_guard(conn, FakeFetch(10.0, 9.0), daily=1.0)
    g.check()
    g.check()
    assert g.usage_today() == 0.0


def test_estado_sobrevive_reinicio(conn):
    g1, _ = make_guard(conn, FakeFetch(10.0, 11.5), daily=1.0)
    g1.check()
    assert g1.check() == "sleep"
    # "Reinicio": guard nuevo sobre la misma DB — sigue quemado sin re-anunciar.
    g2, _ = make_guard(conn, FakeFetch(11.5), daily=1.0)
    assert g2.check() is None
    assert g2.burned is True


def test_estado_corrupto_regenera(conn):
    d.kv_set(conn, "budget_state", "{no es json")
    g, _ = make_guard(conn, FakeFetch(10.0), daily=1.0)
    assert g.check() is None and g.burned is False


def test_dia_nuevo_despierta_aunque_credits_este_caido(conn):
    """Regresión: despertar exigía una lectura EXITOSA de /credits — con el
    endpoint caído (key rotada, provider sin /credits, outage), un estado
    sleeping era PERMANENTE y el bot no corría nada, menciones incluidas.
    Día nuevo = presupuesto nuevo: se despierta con o sin medición."""
    g, days = make_guard(conn, FakeFetch(10.0, 11.5, None), daily=1.0)
    g.check()                          # snapshot
    assert g.check() == "sleep"        # quemado
    days["value"] = "2026-07-20"       # rollover con /credits caído (None)
    assert g.check() == "wake"         # despierta igual
    assert g.burned is False


def test_dia_nuevo_quemado_de_verdad_vuelve_a_dormir(conn):
    """El despertar incondicional no regala presupuesto: si al medir el día
    nuevo el gasto ya supera el tope, el mismo pase lo vuelve a dormir."""
    g, days = make_guard(conn, FakeFetch(10.0, 11.5, 20.0, 21.5), daily=1.0)
    g.check()
    assert g.check() == "sleep"
    days["value"] = "2026-07-20"
    assert g.check() == "wake"         # rollover: snapshot 20.0, gasto 0
    assert g.check() == "sleep"        # gasto 1.5 ≥ 1.0 → duerme de nuevo
    assert g.burned is True


def test_daily_usd_cero_deshabilita_con_warning(conn, caplog):
    """Regresión: daily_usd=0 editado a mano en settings hacía `0 >= 0` = True
    desde el primer check del día — dormido para siempre sin salida."""
    import logging
    with caplog.at_level(logging.WARNING, logger="botata.budget"):
        g, _ = make_guard(conn, FakeFetch(10.0), daily=0)
    assert g.enabled is False
    assert g.check() is None and g.burned is False
    assert "DESHABILITADO" in caplog.text
