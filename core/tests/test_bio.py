"""Tests de la bio automática: run_bio_pass (prompteable, kv bio_current) y
set_bio de los canales (Discord vía PATCH /applications/@me)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b
import db as d
from channels import DiscordChannel
from test_channels import FakeDiscordHttp, FakeResp


@pytest.fixture
def conn(tmp_path):
    c = d.init_db(tmp_path / "bio.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _prompts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "PROMPTS_DIR", tmp_path)
    (tmp_path / "bio.md").write_text("mostrá tu humor actual\n", encoding="utf-8")


class FakeChannel:
    def __init__(self, ok=True):
        self.bios, self.ok = [], ok

    def set_bio(self, text):
        self.bios.append(text)
        return self.ok


class FakeChatLLM:
    def __init__(self, text="soy botata, hoy ando eufórica ⚡", fail=False):
        self.text, self.fail = text, fail
        self.systems = []

    def chat(self, messages, **kw):
        if self.fail:
            raise RuntimeError("LLM caído")
        self.systems.append(messages[0]["content"])
        return self.text


def _patch_llm(monkeypatch, llm):
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)


def test_bio_genera_aplica_y_guarda(conn, monkeypatch):
    llm, ch = FakeChatLLM(), FakeChannel()
    _patch_llm(monkeypatch, llm)
    out = b.run_bio_pass(ch, router=None, conn=conn)
    assert out == "soy botata, hoy ando eufórica ⚡"
    assert ch.bios == [out]
    assert d.kv_get(conn, "bio_current") == out
    assert "mostrá tu humor actual" in llm.systems[0]  # bio.md entró al prompt


def test_bio_sin_cambios_no_toca_la_red(conn, monkeypatch):
    llm, ch = FakeChatLLM(), FakeChannel()
    _patch_llm(monkeypatch, llm)
    b.run_bio_pass(ch, router=None, conn=conn)
    assert b.run_bio_pass(ch, router=None, conn=conn) is None
    assert len(ch.bios) == 1  # la segunda no llamó a set_bio


def test_bio_instructions_puntuales_pisan_el_archivo(conn, monkeypatch):
    llm, ch = FakeChatLLM(), FakeChannel()
    _patch_llm(monkeypatch, llm)
    b.run_bio_pass(ch, router=None, conn=conn, instructions="poné solo tu nombre")
    assert "poné solo tu nombre" in llm.systems[0]
    assert "mostrá tu humor actual" not in llm.systems[0]


def test_bio_sin_archivo_ni_instrucciones_no_llama_llm(conn, monkeypatch, tmp_path):
    (tmp_path / "bio.md").unlink()
    llm, ch = FakeChatLLM(), FakeChannel()
    _patch_llm(monkeypatch, llm)
    assert b.run_bio_pass(ch, router=None, conn=conn) is None
    assert llm.systems == [] and ch.bios == []


def test_bio_llm_caido_no_rompe_ni_aplica(conn, monkeypatch):
    ch = FakeChannel()
    _patch_llm(monkeypatch, FakeChatLLM(fail=True))
    assert b.run_bio_pass(ch, router=None, conn=conn) is None
    assert ch.bios == []


def test_bio_fallo_del_canal_no_guarda_kv(conn, monkeypatch):
    ch = FakeChannel(ok=False)
    _patch_llm(monkeypatch, FakeChatLLM())
    assert b.run_bio_pass(ch, router=None, conn=conn) is None
    assert d.kv_get(conn, "bio_current") is None  # se reintenta al próximo pase


def test_bio_respeta_limite_del_canal(conn, monkeypatch):
    monkeypatch.setattr(b, "CHANNEL", "bluesky")  # límite 256
    largo = "bio larguísima. " * 40
    ch = FakeChannel()
    _patch_llm(monkeypatch, FakeChatLLM(text=largo))
    out = b.run_bio_pass(ch, router=None, conn=conn)
    assert out is not None and len(out) <= 256


# ─── set_bio de DiscordChannel: PATCH /applications/@me ──────────────────────
class FakeDiscordHttpBio(FakeDiscordHttp):
    def __init__(self):
        super().__init__()
        self.patched = []

    def request(self, method, path, params=None, json=None, files=None, data=None):
        if method == "PATCH" and path == "/applications/@me":
            self.patched.append(json)
            return FakeResp({"id": "app", "description": json["description"]})
        return super().request(method, path, params=params, json=json,
                               files=files, data=data)


def test_discord_set_bio_patchea_application():
    http = FakeDiscordHttpBio()
    ch = DiscordChannel("tok", ["111"], http=http)
    assert ch.set_bio("bot mafioso de discord") is True
    assert http.patched == [{"description": "bot mafioso de discord"}]


def test_discord_set_bio_error_devuelve_false():
    http = FakeDiscordHttp()  # sin ruta PATCH → 404
    ch = DiscordChannel("tok", ["111"], http=http)
    assert ch.set_bio("x") is False


# ─── la bio es una RUTINA, no una tarea del motor ───────────────────────────
def test_update_bio_disponible_para_rutinas():
    """routines/bio.md llama a update_bio en su fase de tools: sin este scope,
    la bio no tendría cómo actualizarse sola."""
    from tools import Scope
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "update_bio" in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
    assert "update_bio" in [t.name for t in reg.available(Scope.ADMIN)]
    # nunca pública: un usuario cualquiera no le reescribe el perfil al bot
    assert "update_bio" not in [t.name for t in reg.available(Scope.REPLY)]


def test_la_tarea_bio_ya_no_existe():
    from scheduler import PeriodicTask, apply_tasks_config
    tareas = [PeriodicTask("routines", lambda: None)]
    apply_tasks_config(tareas, {"bio": {"enabled": True}})   # settings viejo
    assert [t.name for t in tareas] == ["routines"]          # se ignora, no rompe


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
