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


# ═══ T44 fase 2: las dos vías de extensión ═══════════════════════════════════
# ─── (a) plugin de la instancia: connectors/*.py ────────────────────────────
_PLUGIN_OK = '''
CONNECTOR = {"id": "lemmy", "label": "Lemmy", "color": "#00bc8c", "initial": "L",
             "placeholder": "comunidad@instancia", "tool": "get_latest_media",
             "help": "Comunidades de Lemmy.", "live": True}


def fetch(source, limit=15):
    return [{"image_url": "https://x/1.jpg", "url": "https://l/1",
             "title": "post", "source": source}]
'''

_PLUGIN_SIN_FETCH = '''CONNECTOR = {"id": "roto", "label": "Roto"}'''
_PLUGIN_EXPLOTA = '''raise RuntimeError("me rompi al importar")'''


@pytest.fixture()
def limpio():
    """Deja el registro como estaba: es estado de módulo."""
    antes_ids = [x.id for x in c.CONNECTORS]
    yield
    for cid in [x.id for x in c.CONNECTORS if x.id not in antes_ids]:
        c.CONNECTORS.remove(c.by_id(cid))
        c._BY_ID.pop(cid, None)
        c._FETCHERS.pop(cid, None)


def _escribir(tmp_path, nombre, cuerpo):
    d = tmp_path / "connectors"
    d.mkdir(exist_ok=True)
    (d / nombre).write_text(cuerpo, encoding="utf-8")
    return d


def test_plugin_de_instancia_se_carga_entero(tmp_path, limpio):
    d = _escribir(tmp_path, "lemmy.py", _PLUGIN_OK)
    assert c.load_instance_plugins(d) == ["lemmy"]
    con = c.by_id("lemmy")
    assert con.label == "Lemmy" and con.live is True
    assert c.fetcher("lemmy")("mi/comunidad")[0]["url"] == "https://l/1"
    assert "lemmy" in c.all_ids()                     # queda como tipo de fuente válido


def test_plugin_sin_fetch_se_ignora(tmp_path, limpio):
    d = _escribir(tmp_path, "roto.py", _PLUGIN_SIN_FETCH)
    assert c.load_instance_plugins(d) == []
    assert c.by_id("roto") is None


def test_plugin_que_explota_no_tumba_el_arranque(tmp_path, limpio):
    d = _escribir(tmp_path, "bomba.py", _PLUGIN_EXPLOTA)
    _escribir(tmp_path, "lemmy.py", _PLUGIN_OK)
    assert c.load_instance_plugins(d) == ["lemmy"]    # el bueno igual entra


def test_carpeta_inexistente_no_rompe(tmp_path):
    assert c.load_instance_plugins(tmp_path / "no-existe") == []


def test_un_plugin_no_puede_declararse_core(tmp_path, limpio):
    d = _escribir(tmp_path, "vivo.py", _PLUGIN_OK.replace('"live": True', '"core": True'))
    c.load_instance_plugins(d)
    assert c.by_id("lemmy").core is False             # core es solo de los built-in
    assert c.is_enabled("lemmy", {"CONNECTORS": {"lemmy": {"enabled": False}}}) is False


# ─── (b) un server MCP declarado como conector ──────────────────────────────
def test_mcp_se_registra_como_conector(limpio):
    llamadas = []

    def _call(tool, args):
        llamadas.append((tool, args))
        return [{"image_url": "https://x/a.jpg", "url": "https://l/a", "title": "t"}]

    cfg = {"connector": {"id": "lemmy", "label": "Lemmy", "fetch_tool": "get_posts",
                         "arg": "community"}}
    assert c.register_from_mcp("lemmy", cfg, _call) == "lemmy"
    items = c.fetcher("lemmy")("linux@lemmy.ml", limit=5)
    assert llamadas == [("get_posts", {"community": "linux@lemmy.ml", "limit": 5})]
    assert items[0]["url"] == "https://l/a"


def test_mcp_sin_bloque_connector_no_registra_nada(limpio):
    assert c.register_from_mcp("reddit", {"transport": "stdio"}, lambda *a: []) is None
    assert c.register_from_mcp("x", {"connector": {"id": "x"}}, lambda *a: []) is None


def test_mcp_completa_los_defaults_de_la_ui(limpio):
    c.register_from_mcp("lemmy", {"connector": {"id": "lemmy", "fetch_tool": "t"}},
                        lambda *a: [])
    con = c.by_id("lemmy")
    assert con.label == "Lemmy" and con.initial == "L" and con.color.startswith("#")
    assert con.tool == "get_latest_media" and con.live is True


def test_mcp_caido_devuelve_vacio_sin_romper(limpio):
    def _call(tool, args):
        raise RuntimeError("server muerto")
    c.register_from_mcp("lemmy", {"connector": {"id": "lemmy", "fetch_tool": "t"}}, _call)
    assert c.fetcher("lemmy")("x") == []
