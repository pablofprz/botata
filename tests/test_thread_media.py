"""Tests de lectura de media del hilo (video/GIF/imagen).

El bot describe con vision el frame/thumbnail de la media de los posts del hilo
para no verlos vacíos. Acá el describidor vision va mockeado (sin red ni LLM):
el foco es el aplanado de embeds (`_iter_media_views`), el formateo de etiquetas
y el cableo en `_thread_context` / `get_thread_info` / `get_mention_by_uri`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402


class O:
    """Objeto anónimo con atributos arbitrarios (fake de los views del SDK)."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ─── _iter_media_views: aplanado de embeds ────────────────────────────────────

def test_iter_images():
    e = O(py_type="app.bsky.embed.images#view",
          images=[O(fullsize="https://cdn/x.jpg", thumb="t", alt="un gato")])
    assert b._iter_media_views(e) == [
        {"kind": "imagen", "image_url": "https://cdn/x.jpg", "alt": "un gato"}
    ]


def test_iter_video_uses_thumbnail():
    e = O(py_type="app.bsky.embed.video#view", thumbnail="https://cdn/v.jpg", alt="")
    out = b._iter_media_views(e)
    assert out == [{"kind": "video", "image_url": "https://cdn/v.jpg", "alt": ""}]


def test_iter_external_gif_detected():
    e = O(py_type="app.bsky.embed.external#view",
          external=O(uri="https://media.tenor.com/abc.gif", title="cat dancing", thumb="https://cdn/g.jpg"))
    out = b._iter_media_views(e)
    assert out[0]["kind"] == "GIF"
    assert out[0]["title"] == "cat dancing"


def test_iter_external_plain_link():
    e = O(py_type="app.bsky.embed.external#view",
          external=O(uri="https://example.com/nota", title="Nota", thumb=None))
    out = b._iter_media_views(e)
    assert out[0]["kind"] == "enlace"
    assert out[0]["image_url"] is None


def test_iter_record_with_media_recurses():
    vid = O(py_type="app.bsky.embed.video#view", thumbnail="https://cdn/v.jpg", alt="")
    e = O(py_type="app.bsky.embed.recordWithMedia#view", media=vid, record=None)
    assert b._iter_media_views(e)[0]["kind"] == "video"


def test_iter_quote_only_ignored():
    e = O(py_type="app.bsky.embed.record#view", record=None)
    assert b._iter_media_views(e) == []


def test_iter_none_and_cap():
    assert b._iter_media_views(None) == []
    many = O(py_type="app.bsky.embed.images#view",
             images=[O(fullsize=f"u{i}", thumb=None, alt="") for i in range(10)])
    assert len(b._iter_media_views(many)) == b._MEDIA_MAX_ITEMS


# ─── formateo de etiquetas ────────────────────────────────────────────────────

def test_format_pieces():
    assert b._format_media_piece("imagen", "un perro", "") == "[imagen: un perro]"
    assert b._format_media_piece("imagen", "", "alt") == "[imagen: alt]"
    assert b._format_media_piece("imagen", "", "") == "[imagen adjunta]"
    assert b._format_media_piece("video", "salta", "") == "[video, primer frame: salta]"
    assert b._format_media_piece("video", "", "") == "[video adjunto]"
    assert b._format_media_piece("GIF", "still", "titulo") == "[GIF: titulo — still]"
    assert b._format_media_piece("GIF", "", "") == "[GIF adjunto]"
    assert b._format_media_piece("enlace", "", "titulo") == "[enlace: titulo]"


def test_sniff_mime():
    assert b._sniff_image_mime(b"\xff\xd8\xff\xe0x") == "image/jpeg"
    assert b._sniff_image_mime(b"\x89PNG\r\n\x1a\nx") == "image/png"
    assert b._sniff_image_mime(b"GIF89a...") == "image/gif"
    assert b._sniff_image_mime(b"RIFF1234WEBPx") == "image/webp"


# ─── describer (vision mockeado) ──────────────────────────────────────────────

def _describer(monkeypatch_desc="D"):
    class FakeVision:
        def chat(self, messages, **kw):
            return "descripción del modelo"
    # cortamos la bajada de bytes: describimos por el basename de la URL
    orig = b._vision_describe_url
    b._vision_describe_url = lambda llm, url: f"{monkeypatch_desc}({url.split('/')[-1]})"
    d = b.make_media_describer(FakeVision())
    return d, orig


def test_make_media_describer_video():
    desc, orig = _describer()
    try:
        e = O(py_type="app.bsky.embed.video#view", thumbnail="https://cdn/v.jpg", alt="")
        out = desc(O(embed=e))
        assert out == "[video, primer frame: D(v.jpg)]"
    finally:
        b._vision_describe_url = orig


def test_make_media_describer_no_media():
    desc, orig = _describer()
    try:
        assert desc(O(embed=None)) == ""
    finally:
        b._vision_describe_url = orig


# ─── cableo en BskyClient ─────────────────────────────────────────────────────

def _bare_client():
    bc = b.BskyClient.__new__(b.BskyClient)
    bc._media_describer = None
    bc.handle = "bot.test"
    return bc


def test_describe_media_noop_without_describer():
    bc = _bare_client()
    assert bc._describe_media(O(embed=O(py_type="app.bsky.embed.video#view"))) == ""


def test_thread_context_annotates_parents():
    bc = _bare_client()
    bc.set_media_describer(lambda pv: "[imagen: un gráfico]"
                           if getattr(getattr(pv, "embed", None), "py_type", "") ==
                           "app.bsky.embed.images#view" else "")
    imgp = O(py_type="app.bsky.embed.images#view")
    root = O(author=O(handle="alice.test"), uri="at://root", cid="c1",
             record=O(text="miren esto"), embed=imgp)
    leaf = O(post=O(author=O(handle="bob.test"), uri="at://leaf", cid="c2",
                    record=O(text="che"), embed=None),
             parent=O(post=root, parent=None))
    ctx, ru, rc = bc._thread_context(leaf)
    assert "alice.test: miren esto [imagen: un gráfico]" in ctx
    assert ru == "at://root"


def test_describe_media_never_raises():
    bc = _bare_client()
    def boom(pv):
        raise RuntimeError("x")
    bc.set_media_describer(boom)
    assert bc._describe_media(O(embed=None)) == ""
