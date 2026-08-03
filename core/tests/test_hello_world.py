"""Presentación inicial (hello world): el primer y único posteo del bot.

Dos mitades: (1) si falta algo por configurar, el bot NO se presenta y dice qué
falta —presentarse como "un bot comunitario deliberadamente neutro" es peor que
no presentarse—; (2) el posteo sale de lo configurado (personalidad, nombre,
comunidad), no de un texto fijo, y se publica una sola vez por instancia.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402


class CanalFalso:
    def __init__(self):
        self.posted: list[str] = []

    def post(self, text, limit=295, media_path=None, target=None):
        self.posted.append(text)
        return f"at://bot/{len(self.posted)}"


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "hello.db")


@pytest.fixture()
def configurado(monkeypatch, tmp_path):
    """Una instancia con TODO lo que la presentación necesita."""
    monkeypatch.setattr(b, "BOT_NAME", "Botata")
    monkeypatch.setattr(b, "COMMUNITY_NAME", "Bluesky Argentina")
    monkeypatch.setattr(b, "BSKY_HANDLE", "botata.bsky.social")
    monkeypatch.setattr(b, "ADMIN_HANDLE", "ppolci.com")
    monkeypatch.setattr(b, "CHANNEL", "bluesky")
    monkeypatch.setenv("BSKY_PASSWORD", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "SOUL.md").write_text("Sos un bot chinchudo del conurbano.", encoding="utf-8")
    monkeypatch.setattr(b, "CONTEXT_DIR", ctx)
    return ctx


@pytest.fixture()
def llm(monkeypatch):
    """RoleLLM falso: devuelve un texto fijo y guarda el system prompt recibido."""
    visto = {}

    class _LLM:
        def __init__(self, router, role):
            visto["role"] = role

        def chat(self, messages):
            visto["system"] = messages[0]["content"]
            return '"hola, soy Botata"'

    monkeypatch.setattr(b, "RoleLLM", _LLM)
    return visto


# ─── "si falta algo, preguntalo" ────────────────────────────────────────────
def test_sin_configurar_no_postea_y_dice_que_falta(monkeypatch, conn, tmp_path):
    monkeypatch.setattr(b, "BOT_NAME", "")
    monkeypatch.setattr(b, "COMMUNITY_NAME", "")
    monkeypatch.setattr(b, "CONTEXT_DIR", tmp_path / "vacio")
    canal = CanalFalso()
    r = b.run_hello_world(canal, None, conn, publish=True)
    assert r["ok"] is False
    assert canal.posted == []                       # no se posteó nada
    faltan = " ".join(r["faltantes"])
    assert "Cómo se llama" in faltan and "comunidad" in faltan
    assert "Personalidad" in faltan                 # SOUL ausente


def test_el_soul_plantilla_cuenta_como_falta(configurado, conn):
    (configurado / "SOUL.md").write_text(
        "You are a community bot... you are deliberately neutral.", encoding="utf-8")
    faltan = " ".join(b.hello_world_pendientes())
    assert "plantilla neutra" in faltan


def test_configurado_no_falta_nada(configurado):
    assert b.hello_world_pendientes() == []


def test_discord_sin_canal_no_sabe_donde_presentarse(configurado, monkeypatch):
    monkeypatch.setattr(b, "CHANNEL", "discord")
    monkeypatch.setattr(b, "DISCORD_CHANNEL_IDS", [])
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    assert any("canal de Discord" in f for f in b.hello_world_pendientes())


# ─── El borrador sale de lo configurado ─────────────────────────────────────
def test_el_borrador_no_postea_y_usa_la_identidad(configurado, conn, llm):
    canal = CanalFalso()
    r = b.run_hello_world(canal, None, conn, publish=False)
    assert r["ok"] and r["text"] == "hola, soy Botata"   # sin las comillas del LLM
    assert canal.posted == []                            # borrador = no se publica
    assert "chinchudo del conurbano" in llm["system"]    # el SOUL manda
    assert "Botata" in llm["system"] and "Bluesky Argentina" in llm["system"]
    assert "PRIMER mensaje" in llm["system"]


def test_las_instrucciones_salen_del_archivo_de_la_instancia(configurado, conn, llm,
                                                             monkeypatch, tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "presentacion.md").write_text("Presentate en verso.", encoding="utf-8")
    monkeypatch.setattr(b, "PROMPTS_DIR", prompts)
    b.run_hello_world(CanalFalso(), None, conn, publish=False)
    assert "Presentate en verso." in llm["system"]


# ─── Publicar: una sola vez ─────────────────────────────────────────────────
def test_publica_y_queda_registrado(configurado, conn, llm):
    canal = CanalFalso()
    r = b.run_hello_world(canal, None, conn, publish=True)
    assert r["ok"] and r["uri"] == "at://bot/1"
    assert canal.posted == ["hola, soy Botata"]
    assert d.kv_get(conn, b._HELLO_KV).endswith("at://bot/1")
    fila = conn.execute("SELECT text FROM bot_posts WHERE uri='at://bot/1'").fetchone()
    assert fila["text"] == "hola, soy Botata"


def test_no_se_presenta_dos_veces(configurado, conn, llm):
    canal = CanalFalso()
    b.run_hello_world(canal, None, conn, publish=True)
    r = b.run_hello_world(canal, None, conn, publish=True)
    assert r["ok"] is False and r["ya_posteado"]
    assert len(canal.posted) == 1
    # …salvo que el admin lo fuerce a propósito
    assert b.run_hello_world(canal, None, conn, publish=True, force=True)["ok"]
    assert len(canal.posted) == 2


def test_el_admin_puede_editar_el_texto(configurado, conn, llm):
    canal = CanalFalso()
    r = b.run_hello_world(canal, None, conn, text="  lo escribo yo  ", publish=True)
    assert r["ok"] and canal.posted == ["lo escribo yo"]
    assert "system" not in llm                       # ni se llamó al LLM


def test_si_el_posteo_falla_no_queda_marcado(configurado, conn, llm):
    class Roto(CanalFalso):
        def post(self, *a, **k):
            raise RuntimeError("token vencido")

    r = b.run_hello_world(Roto(), None, conn, publish=True)
    assert r["ok"] is False and "token vencido" in " ".join(r["faltantes"])
    assert d.kv_get(conn, b._HELLO_KV) is None       # se puede reintentar
