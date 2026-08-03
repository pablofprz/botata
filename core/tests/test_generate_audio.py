"""Tests de la generación de audio (TTS · ElevenLabs): la tool generate_audio.

Espejo de test_generate_image, con dos diferencias de diseño: no pasa por el
router (ElevenLabs no habla OpenAI — se mockea `_elevenlabs_tts`) y el canal
tiene que declarar SUPPORTS_AUDIO, porque el .ogg viaja por la plomería
genérica de media y no todo canal sabe subirlo. Nada acá toca la red.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from tools import ToolContext  # noqa: E402

_OGG = b"OggS_opus_falso"

_ESTADO = {"mention_uri": "uri://mencion", "mention_cid": "cid1",
           "thread_root_uri": "uri://raiz", "thread_root_cid": "cid0"}


class CanalConAudio:
    SUPPORTS_AUDIO = True


class CanalSinAudio:
    pass


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """Key presente, TTS mockeado y directorio de salida efímero."""
    monkeypatch.setattr(b, "ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(b, "_AUDIO_DIR", tmp_path / "generated_audio")
    monkeypatch.setattr(b, "settings", {**b.settings, "AUDIO_GEN": {"voice_id": "v"}})
    llamadas: list[tuple] = []

    def tts_falso(texto, cfg):
        llamadas.append((texto, cfg))
        return _OGG

    monkeypatch.setattr(b, "_elevenlabs_tts", tts_falso)
    return llamadas


_CTX = ToolContext(state={}, conn=None)


def _tool(canal=None):
    return b._make_generate_audio_tool(CanalConAudio() if canal is None else canal)


def test_genera_y_adjunta(entorno):
    out = _tool()({"text": "hola, soy botata"}, _CTX)
    assert out.image_path and Path(out.image_path).read_bytes() == _OGG
    assert Path(out.image_path).suffix == ".ogg"
    assert entorno[0][0] == "hola, soy botata"


def test_texto_vacio(entorno):
    out = _tool()({"text": "  "}, _CTX)
    assert out.image_path is None and "necesito el texto" in out.text
    assert not entorno


def test_sin_api_key(entorno, monkeypatch):
    monkeypatch.setattr(b, "ELEVENLABS_API_KEY", None)
    out = _tool()({"text": "x"}, _CTX)
    assert out.image_path is None and "no tengo generación de audio" in out.text


def test_canal_sin_soporte(entorno):
    """Bluesky (o cualquier canal sin el flag) rechazaría el .ogg como imagen:
    la tool se niega antes, en vez de romper el reply entero."""
    for handler in (b._make_generate_audio_tool(CanalSinAudio()),
                    b._make_generate_audio_tool(None)):
        out = handler({"text": "x"}, _CTX)
        assert out.image_path is None and "no puedo mandar audios" in out.text
    assert not entorno


def test_sin_voz_configurada(entorno, monkeypatch):
    """AUDIO_GEN sin voice_id es el estado normal antes de elegirla en la UI:
    la tool avisa dónde se configura en vez de fallar en el proveedor."""
    monkeypatch.setattr(b, "settings", {**b.settings, "AUDIO_GEN": {}})
    out = _tool()({"text": "x"}, _CTX)
    assert out.image_path is None and "voz configurada" in out.text
    assert not entorno


def test_texto_muy_largo(entorno, monkeypatch):
    """ElevenLabs cobra por carácter: se rechaza, no se trunca."""
    monkeypatch.setattr(b, "settings", {**b.settings, "AUDIO_GEN": {"voice_id": "v", "max_chars": 10}})
    out = _tool()({"text": "una frase que se pasa del tope"}, _CTX)
    assert out.image_path is None and "muy largo" in out.text
    assert not entorno


def test_tope_diario(entorno, monkeypatch):
    monkeypatch.setattr(b, "settings", {**b.settings, "AUDIO_GEN": {"voice_id": "v", "max_per_day": 2}})
    tool = _tool()
    assert tool({"text": "uno"}, _CTX).image_path
    assert tool({"text": "dos"}, _CTX).image_path
    tercero = tool({"text": "tres"}, _CTX)
    assert tercero.image_path is None and "para hoy" in tercero.text
    assert len(entorno) == 2            # ni siquiera se llamó al proveedor


def test_tope_por_hilo(entorno, monkeypatch, tmp_path):
    """Misma lección que las imágenes (2026-08-02): en un hilo donde los audios
    son el tema, el modelo los encadena. El prompt pide, el código impide."""
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "hilo.db")
    monkeypatch.setattr(b, "settings", {**b.settings, "AUDIO_GEN": {"voice_id": "v", "max_per_thread": 2}})
    tool = _tool()
    ctx = ToolContext(state=dict(_ESTADO), conn=conn)
    assert tool({"text": "uno"}, ctx).image_path
    assert tool({"text": "dos"}, ctx).image_path
    tercero = tool({"text": "tres"}, ctx)
    assert tercero.image_path is None and "en esta charla" in tercero.text
    assert len(entorno) == 2
    # otro hilo arranca de cero
    otro = ToolContext(state={**_ESTADO, "thread_root_uri": "uri://otra-charla"}, conn=conn)
    assert tool({"text": "cuatro"}, otro).image_path


def test_tope_cero_es_sin_tope(entorno, monkeypatch):
    monkeypatch.setattr(b, "settings", {**b.settings,
                                        "AUDIO_GEN": {"voice_id": "v", "max_per_day": 0, "max_chars": 0}})
    tool = _tool()
    for i in range(3):
        assert tool({"text": f"n{i}"}, _CTX).image_path


def test_caida_del_proveedor(entorno, monkeypatch):
    def tts_roto(texto, cfg):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(b, "_elevenlabs_tts", tts_roto)
    out = _tool()({"text": "x"}, _CTX)
    assert out.image_path is None and "no pude generar el audio" in out.text


def test_registrada_con_scope_admin_por_default():
    """Nace admin como generate_image (cada audio se paga); ampliar a reply es
    opt-in de la instancia, como hace botata-rancher en su settings."""
    reg = b.build_tool_registry({}, bsky=CanalConAudio())
    tool = reg.get("generate_audio")
    assert tool is not None and tool.scopes == frozenset({"admin"})
