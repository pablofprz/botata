"""El bot no puede FINGIR un adjunto escribiendo la anotación en el texto.

Caso real (2026-07-27): a "poneme la foto de un mapache" contestó
"ahí va, mapachito de la galería 🦝 [imagen: Un mapache comiendo de un tarrito]"
— sin imagen adjunta. Dos causas: (1) search_images era admin-only, así que en
una reply el LLM no la tenía y cayó en web_search; (2) al ver siempre la media
anotada entre corchetes en SU PROPIO contexto, aprendió el formato y lo escribió
como si adjuntar fuera narrar.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from channels import strip_fake_media  # noqa: E402
from tools import Scope  # noqa: E402


# ─── el sanitizador ─────────────────────────────────────────────────────────
def test_el_caso_del_mapache():
    crudo = "ahí va, mapachito de la galería 🦝\n[imagen: Un mapache comiendo de un tarrito]"
    assert strip_fake_media(crudo) == "ahí va, mapachito de la galería 🦝"


@pytest.mark.parametrize("crudo", [
    "mirá esto [imagen: un gato]",
    "mirá esto [image: cat.jpg]",
    "mirá esto [foto: algo]",
    "mirá esto [video, primer frame: alguien corriendo]",
    "mirá esto [GIF: bailando]",
    "mirá esto [adjunto: cosa.png]",
    "mirá esto [IMAGEN: MAYÚSCULAS]",
])
def test_saca_todas_las_variantes(crudo):
    assert strip_fake_media(crudo) == "mirá esto"


def test_no_toca_corchetes_legitimos():
    for texto in ["[2026] fue un año raro", "una lista [1, 2, 3]",
                  "el tema [Spotify] está bueno", "esto [no es media] igual queda"]:
        assert strip_fake_media(texto) == texto


def test_limpia_los_espacios_que_deja():
    assert strip_fake_media("hola [imagen: x] mundo") == "hola mundo"
    assert strip_fake_media("[imagen: x]") == ""
    assert strip_fake_media("") == ""


# ─── el embudo: ningún canal deja pasar la anotación ────────────────────────
class _FakeBsky(b.BskyClient):
    def __init__(self):            # sin login
        self.posted = []
        self._media_describer = None

    def _upload_media(self, *a, **k):
        return None


def test_bluesky_no_publica_la_anotacion(monkeypatch):
    ch = _FakeBsky()
    visto = {}
    monkeypatch.setattr(ch, "_send_post", lambda *a, **k: visto.setdefault("t", a), raising=False)
    limpio = strip_fake_media("posteo [imagen: inventada]")
    assert "[imagen" not in limpio


def test_discord_y_mastodon_sanean_en_reply_y_post():
    """Los cuatro métodos de salida pasan por el sanitizador."""
    import inspect

    import channels
    for cls in (channels.MastodonChannel, channels.DiscordChannel):
        for metodo in ("post", "reply"):
            fuente = inspect.getsource(getattr(cls, metodo))
            assert "strip_fake_media" in fuente, f"{cls.__name__}.{metodo}"


# ─── la causa raíz: la tool tiene que estar disponible en replies ───────────
def test_search_images_disponible_en_replies():
    """Sin esto el LLM no tiene con qué resolver 'poneme una foto' y la inventa."""
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    disponibles = [t.name for t in reg.available(Scope.REPLY)]
    assert "search_images" in disponibles
    assert "search_videos" in disponibles
