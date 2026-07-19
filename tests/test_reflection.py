"""Tests del pase de reflexión (T6 cierre, run_reflection_pass).

Destila lecciones conductuales de la actividad reciente del bot. Spy sobre
upsert_lesson para no cargar bge-m3; user_exists corre de verdad (barato).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402


class FakeLLM:
    """Reemplaza RoleLLM: devuelve una reflexión fija y cuenta llamadas."""

    def __init__(self, reflection: dict):
        self._reflection = reflection
        self.calls = 0

    def complete(self, system, user, model_cls=None):
        self.calls += 1
        self.last_system = system
        self.last_user = user
        return b.LessonsReflection(**self._reflection)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conn = d.init_db(tmp_path / "reflect_test.db")
    conn.execute("INSERT OR IGNORE INTO users(handle) VALUES ('ppolci.com')")  # único con perfil
    conn.commit()
    saved: list[tuple[str, str]] = []

    def spy_upsert(c, lesson_text, scope="community", **kw):
        saved.append((lesson_text, scope))
        return len(saved)  # id ficticio (no-None → cuenta como insert)

    monkeypatch.setattr(b.dbmod, "upsert_lesson", spy_upsert)

    def run(reflection: dict):
        llm = FakeLLM(reflection)
        monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
        b.run_reflection_pass(router=None, conn=conn)
        return llm, saved

    return conn, run


def _seed_activity(conn, n: int, handle: str = "ppolci.com") -> None:
    for i in range(n):
        b.log_bot_post(conn, uri=f"at://bot/{i}", in_reply_to=f"at://x/{i}",
                       reply_to_handle=handle, text=f"respuesta {i}")


def test_poca_actividad_no_llama_llm(env):
    conn, run = env
    _seed_activity(conn, 2)  # < min_activity (4)
    llm, saved = run({"lessons": [{"lesson": "algo"}]})
    assert llm.calls == 0 and saved == []


def test_destila_leccion_de_comunidad(env):
    conn, run = env
    _seed_activity(conn, 6)
    llm, saved = run({"lessons": [{"lesson": "Variar los remates: repitió cierres."}]})
    assert llm.calls == 1
    assert saved == [("Variar los remates: repitió cierres.", "community")]
    assert "respuesta 0" in llm.last_user  # la actividad llegó al prompt


def test_leccion_sobre_usuario_con_perfil_scope_user(env):
    conn, run = env
    _seed_activity(conn, 6)
    _, saved = run({"lessons": [
        {"lesson": "Con Polci conviene el tono absurdo.", "about_handle": "ppolci.com"},
    ]})
    assert saved == [("Con Polci conviene el tono absurdo.", "user:ppolci.com")]


def test_leccion_sobre_usuario_sin_perfil_cae_a_comunidad(env):
    conn, run = env
    _seed_activity(conn, 6)
    _, saved = run({"lessons": [
        {"lesson": "X derrapa a la política; cortar corto.", "about_handle": "random.com"},
    ]})
    assert saved == [("X derrapa a la política; cortar corto.", "community")]


def test_sin_lecciones_no_escribe(env):
    conn, run = env
    _seed_activity(conn, 6)
    llm, saved = run({"lessons": []})
    assert llm.calls == 1 and saved == []


def test_leccion_vacia_se_saltea(env):
    conn, run = env
    _seed_activity(conn, 6)
    _, saved = run({"lessons": [{"lesson": "   "}, {"lesson": "válida"}]})
    assert saved == [("válida", "community")]


def test_lecciones_existentes_van_al_prompt(env):
    conn, run = env
    _seed_activity(conn, 6)
    conn.execute("INSERT INTO lessons(lesson_text, scope) VALUES ('vieja', 'community')")
    conn.commit()
    llm, _ = run({"lessons": []})
    assert "vieja" in llm.last_system
