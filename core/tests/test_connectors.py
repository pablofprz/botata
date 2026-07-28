"""T44 · Catálogo de conectores: una sola declaración para motor, UI y validador."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import config_ui as cu  # noqa: E402
import connectors as c  # noqa: E402


def test_el_motor_y_la_ui_leen_el_mismo_catalogo():
    """El bug que motivó T44: el tipo estaba declarado en 3 lugares a la vez."""
    assert b.SOURCE_TYPES == cu.SOURCE_TYPES == c.all_ids()
    assert "pinterest" in c.all_ids() and "membrilla" in c.all_ids()


def test_cada_conector_declara_lo_que_la_ui_necesita():
    for con in c.CONNECTORS:
        assert con.label and con.color.startswith("#") and con.initial
        assert con.tool and con.help, con.id


def test_catalogo_serializa_para_la_ui():
    cat = {x["id"]: x for x in c.catalog({})}
    assert cat["tumblr"]["needs_env"] == ["TUMBLR_API_KEY"]
    assert cat["pinterest"]["live"] is True
    assert cat["membrilla"]["core"] is True


# ─── toggles: apagar un conector deja sus fuentes inertes ───────────────────
def test_apagar_un_conector_lo_saca_de_juego(monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "pinterest", "category": "arte", "sources": ["u/b"], "enabled": True}])
    monkeypatch.setattr(b, "settings", {})
    assert b.sources_of_type("pinterest", "arte") == ["u/b"]
    monkeypatch.setattr(b, "settings", {"CONNECTORS": {"pinterest": {"enabled": False}}})
    assert b.sources_of_type("pinterest", "arte") == []      # inerte, pero no se borró


def test_los_core_no_se_pueden_apagar():
    apagado = {"CONNECTORS": {"membrilla": {"enabled": False}}}
    assert c.is_enabled("membrilla", apagado) is True        # el motor cuenta con él
    assert c.is_enabled("pinterest", {"CONNECTORS": {"pinterest": {"enabled": False}}}) is False


def test_conector_desconocido_no_esta_habilitado():
    assert c.is_enabled("mastodonte", {}) is False


# ─── validación desde la UI ─────────────────────────────────────────────────
def test_validador_rechaza_conector_inexistente():
    s = {"BOT_HANDLE": "b", "ADMIN_HANDLE": "a", "CONNECTORS": {"yolo": {"enabled": True}}}
    assert any("conector desconocido" in e for e in cu.validate_settings(s))


def test_validador_rechaza_enabled_no_booleano():
    s = {"BOT_HANDLE": "b", "ADMIN_HANDLE": "a", "CONNECTORS": {"rss": {"enabled": "si"}}}
    assert any("booleano" in e for e in cu.validate_settings(s))


def test_validador_acepta_lo_valido():
    s = {"BOT_HANDLE": "b", "ADMIN_HANDLE": "a", "CONNECTORS": {"rss": {"enabled": False}}}
    assert cu.validate_settings(s) == []


# ─── fetchers: el registro resuelve tarde (se puede reemplazar) ─────────────
def test_los_fetchers_en_vivo_estan_registrados(monkeypatch):
    for cid in ("pinterest", "tumblr"):
        assert c.fetcher(cid) is not None
    monkeypatch.setattr(b, "_pinterest_board_items", lambda ref, limit=15: [{"marca": ref}])
    assert c.fetcher("pinterest")("x/y") == [{"marca": "x/y"}]
