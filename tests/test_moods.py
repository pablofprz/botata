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
