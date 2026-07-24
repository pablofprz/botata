"""Tests del sistema de moods (estados de ánimo del bot).

El pase auto (`run_mood_pass`) va con un router mockeado (sin red ni LLM). El foco
es la resolución (`current_mood`) en los 4 modos, la idempotencia por día, el
fallback ante un name alucinado, y el loader de moods/*.md."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
import moods as mm  # noqa: E402


@pytest.fixture()
def conn():
    c = d.init_db(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def restore_config():
    saved = b.MOODS_CONFIG
    yield
    b.MOODS_CONFIG = saved


# ─── loader (moods/*.md reales del repo) ──────────────────────────────────────

def test_loader_reads_repo_moods():
    loaded = mm.load_moods(b.MOODS_DIR)
    assert loaded  # el repo trae moods de ejemplo
    for name, mood in loaded.items():
        assert mood.name == name
        assert mood.description and mood.body


def test_loader_missing_dir(tmp_path):
    assert mm.load_moods(tmp_path / "nope") == {}


def test_loader_skips_disabled_and_broken(tmp_path):
    (tmp_path / "ok.md").write_text("---\nname: ok\ndescription: d\n---\ncuerpo", encoding="utf-8")
    (tmp_path / "off.md").write_text("---\nname: off\ndescription: d\nenabled: false\n---\nx", encoding="utf-8")
    (tmp_path / "broken.md").write_text("sin frontmatter", encoding="utf-8")
    (tmp_path / "README.md").write_text("---\nname: readme\ndescription: d\n---\nx", encoding="utf-8")
    loaded = mm.load_moods(tmp_path)
    assert set(loaded) == {"ok"}


# ─── current_mood: los 4 modos ────────────────────────────────────────────────

def test_disabled_returns_none(conn):
    b.MOODS_CONFIG = {"enabled": False}
    assert b.current_mood(conn) is None
    assert b.mood_line(conn) == ""


def test_manual_fixed(conn):
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual", "manual": {"fixed": "snarky"}}
    m = b.current_mood(conn)
    assert m and m.name == "snarky"
    assert "snarky" in b.mood_line(conn)


def test_manual_fixed_nonexistent(conn):
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual", "manual": {"fixed": "no-existe"}}
    assert b.current_mood(conn) is None


def test_manual_schedule_today(conn):
    key = b._WEEKDAY_KEYS[b.now_ar().weekday()]
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual", "manual": {"schedule": {key: "gloomy"}}}
    m = b.current_mood(conn)
    assert m and m.name == "gloomy"


def test_manual_schedule_unmapped_day(conn):
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual", "manual": {"schedule": {"zzz": "gloomy"}}}
    assert b.current_mood(conn) is None


def test_manual_fixed_beats_schedule(conn):
    key = b._WEEKDAY_KEYS[b.now_ar().weekday()]
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual",
                      "manual": {"fixed": "upbeat", "schedule": {key: "gloomy"}}}
    assert b.current_mood(conn).name == "upbeat"


def test_auto_undecided_returns_none(conn):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto"}
    assert b.current_mood(conn) is None


# ─── run_mood_pass (auto) ─────────────────────────────────────────────────────

def _fake_rolellm(monkeypatch, mood, reason):
    class Fake:
        def complete(self, system, user, model):
            return model(mood=mood, reason=reason)
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: Fake())


def test_run_mood_pass_persists(conn, monkeypatch):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto"}
    _fake_rolellm(monkeypatch, "prickly", "me trataron mal")
    b.run_mood_pass(None, conn, force=True)
    st = json.loads(d.kv_get(conn, "mood_state"))
    assert st["mood"] == "prickly" and st["reason"] == "me trataron mal"
    assert b.current_mood(conn).name == "prickly"


def test_run_mood_pass_idempotent_same_day(conn, monkeypatch):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto"}
    _fake_rolellm(monkeypatch, "upbeat", "buena onda")
    b.run_mood_pass(None, conn)                       # decide
    _fake_rolellm(monkeypatch, "gloomy", "cambió")     # no debería re-decidir
    b.run_mood_pass(None, conn)
    assert json.loads(d.kv_get(conn, "mood_state"))["mood"] == "upbeat"


def test_run_mood_pass_hallucinated_name_falls_back(conn, monkeypatch):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto"}
    _fake_rolellm(monkeypatch, "no-existe-este-mood", "x")
    b.run_mood_pass(None, conn, force=True)
    chosen = json.loads(d.kv_get(conn, "mood_state"))["mood"]
    assert chosen in mm.load_moods(b.MOODS_DIR)  # cayó en uno real


def test_run_mood_pass_noop_when_disabled(conn, monkeypatch):
    b.MOODS_CONFIG = {"enabled": False}
    _fake_rolellm(monkeypatch, "upbeat", "x")
    b.run_mood_pass(None, conn, force=True)
    assert d.kv_get(conn, "mood_state") is None


def test_run_mood_pass_noop_when_manual(conn, monkeypatch):
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual", "manual": {"fixed": "upbeat"}}
    _fake_rolellm(monkeypatch, "gloomy", "x")
    b.run_mood_pass(None, conn, force=True)
    assert d.kv_get(conn, "mood_state") is None


# ─── choose_mood: cambio de mood DENTRO del día (tool) ────────────────────────

from tools import ToolContext  # noqa: E402


def _ctx(conn, handle="user.test"):
    return ToolContext(state={"author_handle": handle}, conn=conn)


@pytest.fixture()
def admin_de_test(monkeypatch):
    monkeypatch.setattr(b, "ADMIN_HANDLES", frozenset({"admin.test"}))


def test_choose_mood_cambia_y_persiste(conn, admin_de_test):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto", "susceptibility": 1.0}
    res = b._tool_choose_mood({"mood": "upbeat", "reason": "me pasaron memes"}, _ctx(conn))
    assert "upbeat" in res.text
    st = json.loads(d.kv_get(conn, "mood_state"))
    assert st["mood"] == "upbeat" and st["mode"] == "reactive" and st["changed_at"]
    assert b.current_mood(conn).name == "upbeat"


def test_choose_mood_histeresis_bloquea(conn, admin_de_test):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto",
                      "susceptibility": 1.0, "hysteresis_hours": 2}
    b._tool_choose_mood({"mood": "angry", "reason": "me putearon"}, _ctx(conn))
    res = b._tool_choose_mood({"mood": "upbeat", "reason": "ya fue"}, _ctx(conn))
    assert "hace poco" in res.text
    assert json.loads(d.kv_get(conn, "mood_state"))["mood"] == "angry"


def test_choose_mood_admin_bypasea_histeresis(conn, admin_de_test):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto",
                      "susceptibility": 1.0, "hysteresis_hours": 24}
    b._tool_choose_mood({"mood": "angry", "reason": "x"}, _ctx(conn))
    res = b._tool_choose_mood({"mood": "chill", "reason": "orden"}, _ctx(conn, "admin.test"))
    assert "chill" in res.text
    st = json.loads(d.kv_get(conn, "mood_state"))
    assert st["mood"] == "chill" and st["mode"] == "admin"


def test_choose_mood_susceptibilidad_cero(conn, admin_de_test):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto", "susceptibility": 0}
    res = b._tool_choose_mood({"mood": "upbeat", "reason": "x"}, _ctx(conn))
    assert d.kv_get(conn, "mood_state") is None and "susceptibility" in res.text


def test_choose_mood_apagado_o_manual(conn, admin_de_test):
    b.MOODS_CONFIG = {"enabled": False}
    assert "apagados" in b._tool_choose_mood({"mood": "upbeat", "reason": "x"}, _ctx(conn)).text
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual", "manual": {"fixed": "upbeat"}}
    assert "manual" in b._tool_choose_mood({"mood": "gloomy", "reason": "x"}, _ctx(conn)).text
    assert d.kv_get(conn, "mood_state") is None


def test_choose_mood_name_desconocido(conn, admin_de_test):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto", "susceptibility": 1.0}
    res = b._tool_choose_mood({"mood": "no-existe", "reason": "x"}, _ctx(conn))
    assert "no conozco" in res.text and "upbeat" in res.text  # lista los disponibles
    assert d.kv_get(conn, "mood_state") is None


def test_choose_mood_aproxima_con_matcher(conn, admin_de_test, monkeypatch):
    """'tierno' no existe pero el matcher (LLM lite en prod) lo mapea a chill."""
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto", "susceptibility": 1.0}
    monkeypatch.setattr(b, "_MOOD_MATCHER", lambda name: "chill")
    res = b._tool_choose_mood({"mood": "tierno", "reason": "me pasaron gatitos"}, _ctx(conn))
    assert "chill" in res.text and "tierno" in res.text  # avisa la aproximación
    assert json.loads(d.kv_get(conn, "mood_state"))["mood"] == "chill"


def test_choose_mood_matcher_sin_match_lista_disponibles(conn, admin_de_test, monkeypatch):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto", "susceptibility": 1.0}
    monkeypatch.setattr(b, "_MOOD_MATCHER", lambda name: None)
    res = b._tool_choose_mood({"mood": "zapato", "reason": "x"}, _ctx(conn))
    assert "no conozco" in res.text and "upbeat" in res.text
    assert d.kv_get(conn, "mood_state") is None


def test_default_mood_cuando_nada_resuelve(conn):
    """MOODS.default = base cuando no hay estado del día (auto) ni schedule (manual)."""
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto", "default": "chill"}
    assert b.current_mood(conn).name == "chill"          # sin pase del día → default
    b.MOODS_CONFIG = {"enabled": True, "mode": "manual", "default": "chill",
                      "manual": {"fixed": "", "schedule": {}}}
    assert b.current_mood(conn).name == "chill"          # día sin mapear → default
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto"}
    assert b.current_mood(conn) is None                  # sin default → como antes


def test_choose_mood_reset_vuelve_al_default(conn, admin_de_test):
    b.MOODS_CONFIG = {"enabled": True, "mode": "auto", "default": "chill",
                      "susceptibility": 1.0, "hysteresis_hours": 0}
    b._tool_choose_mood({"mood": "angry", "reason": "me putearon"}, _ctx(conn))
    assert b.current_mood(conn).name == "angry"
    res = b._tool_choose_mood({"mood": "reset", "reason": "orden"}, _ctx(conn, "admin.test"))
    assert "chill" in res.text
    assert d.kv_get(conn, "mood_state") is None
    assert b.current_mood(conn).name == "chill"


def test_choose_mood_schema_tiene_enum(monkeypatch):
    """El enum en el schema previene names alucinados en origen."""
    monkeypatch.setattr(b, "MOODS_CONFIG",
                        {"enabled": True, "mode": "auto", "susceptibility": 0.5})
    reg = b.build_tool_registry()
    enum = reg.get("choose_mood").parameters["properties"]["mood"].get("enum")
    assert enum and "chill" in enum and "upbeat" in enum and "reset" in enum


def test_choose_mood_scopes_segun_config(monkeypatch):
    from tools import Scope
    monkeypatch.setattr(b, "MOODS_CONFIG",
                        {"enabled": True, "mode": "auto", "susceptibility": 0.5})
    reg = b.build_tool_registry()
    assert Scope.REPLY in reg.get("choose_mood").scopes
    monkeypatch.setattr(b, "MOODS_CONFIG",
                        {"enabled": True, "mode": "auto", "susceptibility": 0})
    reg = b.build_tool_registry()
    assert reg.get("choose_mood").scopes == {Scope.ADMIN}


def test_pref_tools_scopes_segun_modo(monkeypatch):
    from tools import Scope
    monkeypatch.setattr(b, "PREFS_MODE", "manual")
    reg = b.build_tool_registry()
    assert reg.get("add_preference").scopes == {Scope.ADMIN}
    assert reg.get("remove_preference").scopes == {Scope.ADMIN}
    monkeypatch.setattr(b, "PREFS_MODE", "add_only")
    reg = b.build_tool_registry()
    assert Scope.REPLY in reg.get("add_preference").scopes
    assert reg.get("remove_preference").scopes == {Scope.ADMIN}
    monkeypatch.setattr(b, "PREFS_MODE", "full_auto")
    reg = b.build_tool_registry()
    assert Scope.REPLY in reg.get("remove_preference").scopes
