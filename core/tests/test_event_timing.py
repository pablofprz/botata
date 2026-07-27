"""Tests de _event_timing_label: la anotación de timing que el heartbeat
calcula en código para que el LLM no haga aritmética de horas."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

from botata import _event_timing_label  # noqa: E402

_NOW = datetime(2026, 7, 25, 18, 0)  # 25/7 18:00 ART


def test_ya_paso():
    assert _event_timing_label("2026-07-25T15:00", _NOW) == " [YA PASÓ]"


def test_empieza_pronto():
    assert _event_timing_label("2026-07-25T18:45", _NOW) == " [empieza en 45 min]"


def test_hoy_mas_tarde():
    assert _event_timing_label("2026-07-25T21:00", _NOW) == " [HOY, faltan ~3 h]"


def test_otro_dia_sin_anotacion():
    assert _event_timing_label("2026-07-28T21:00", _NOW) == ""


def test_dia_entero_sin_hora():
    assert _event_timing_label("2026-07-25", _NOW) == " [HOY, todo el día]"
    assert _event_timing_label("2026-07-26", _NOW) == ""


def test_fecha_invalida_no_rompe():
    assert _event_timing_label("mañana a la nochecita", _NOW) == ""
