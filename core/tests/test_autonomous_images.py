"""Tests de imágenes autónomas + guardrail (T12).

`hybrid_search_image_catalog` va mockeado (evita cargar bge-m3). El foco es el
guardrail de `resolve_catalog_image` y el cableo en `PostFeedNode`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402


def _row(**over):
    base = {"id": 1, "file_path": "scrape/pictures/manual/x.jpg",
            "description": "un gato sorprendido", "category": "meme"}
    base.update(over)
    return base


@pytest.fixture()
def used(monkeypatch):
    marked: list[int] = []
    monkeypatch.setattr(b.dbmod, "mark_image_used", lambda conn, i: marked.append(i))
    return marked


def _mock_search(monkeypatch, rows):
    monkeypatch.setattr(b.dbmod, "hybrid_search_image_catalog", lambda *a, **k: rows)


# ─── guardrail ───────────────────────────────────────────────────────────────
def test_valid_image_resolves_and_marks_used(monkeypatch, used):
    _mock_search(monkeypatch, [_row()])
    # Devuelve path ABSOLUTO (relativo a la raíz del repo) para que bsky.post/reply
    # lo encuentre sin importar el cwd desde el que corra el bot.
    assert b.resolve_catalog_image(None, "gato") == str(b.BASE_DIR / "scrape/pictures/manual/x.jpg")
    assert used == [1]


def test_guardrail_rejects_empty_description(monkeypatch, used):
    _mock_search(monkeypatch, [_row(description="  ")])
    assert b.resolve_catalog_image(None, "gato") is None
    assert used == []  # no la marca como usada


def test_guardrail_rejects_invalid_category(monkeypatch, used):
    _mock_search(monkeypatch, [_row(category="nsfw")])
    assert b.resolve_catalog_image(None, "gato") is None


def test_frame_resolves_to_parent_video(monkeypatch, used, tmp_path):
    # Un frame de tiktok (<id>_f<n>.jpg) cuyo .mp4 padre existe → se postea el VIDEO
    # entero (<id>_0.mp4), no el fotograma suelto.
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)
    vid_dir = tmp_path / "scrape" / "pictures" / "tiktok"
    vid_dir.mkdir(parents=True)
    (vid_dir / "vid_0.mp4").write_bytes(b"fakevideo")
    _mock_search(monkeypatch, [_row(id=9, file_path="scrape/pictures/tiktok/vid_f1.jpg")])
    assert b.resolve_catalog_image(None, "gato") == str(tmp_path / "scrape/pictures/tiktok/vid_0.mp4")
    assert used == [9]


def test_frame_without_video_falls_back(monkeypatch, used, tmp_path):
    # frame cuyo .mp4 no está en disco → se saltea y cae a la siguiente posteable
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)  # no se crea ningún .mp4
    _mock_search(monkeypatch, [
        _row(id=9, file_path="scrape/pictures/tiktok/vid_f1.jpg"),
        _row(id=2, file_path="scrape/pictures/instagram/ok_0.jpg"),
    ])
    assert b.resolve_catalog_image(None, "gato") == str(tmp_path / "scrape/pictures/instagram/ok_0.jpg")
    assert used == [2]  # marca la posteable, nunca el frame


def test_all_frames_without_video_returns_none(monkeypatch, used, tmp_path):
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)
    _mock_search(monkeypatch, [_row(file_path="scrape/pictures/tiktok/vid_f0.jpg")])
    assert b.resolve_catalog_image(None, "gato") is None
    assert used == []


# ─── routing video vs imagen en BskyClient._send_media ───────────────────────
class _FakeAtprotoClient:
    def __init__(self):
        self.video_calls: list = []
        self.image_calls: list = []

    def send_video(self, **kw):
        self.video_calls.append(kw)
        return type("R", (), {"uri": "at://vid/1"})()

    def send_image(self, **kw):
        self.image_calls.append(kw)
        return type("R", (), {"uri": "at://img/1"})()


def _bare_bsky(fake):
    bc = object.__new__(b.BskyClient)  # sin __init__ (evita el login)
    bc._client = fake
    return bc


def test_send_media_routes_mp4_to_send_video(tmp_path):
    fake = _FakeAtprotoClient()
    bc = _bare_bsky(fake)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"fakevideo")
    bc._send_media("mirá este michi", str(vid), facets=None)
    assert len(fake.video_calls) == 1 and not fake.image_calls
    assert fake.video_calls[0]["video"] == b"fakevideo"


def test_send_media_routes_image_to_send_image(tmp_path):
    fake = _FakeAtprotoClient()
    bc = _bare_bsky(fake)
    img = tmp_path / "i.jpg"
    img.write_bytes(b"fakejpeg")
    bc._send_media("un meme", str(img), facets=None)
    assert len(fake.image_calls) == 1 and not fake.video_calls


def test_no_match_returns_none(monkeypatch, used):
    _mock_search(monkeypatch, [])
    assert b.resolve_catalog_image(None, "gato") is None


def test_empty_query_shortcircuits(monkeypatch, used):
    called = {"n": 0}
    monkeypatch.setattr(b.dbmod, "hybrid_search_image_catalog",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])
    assert b.resolve_catalog_image(None, "   ") is None
    assert called["n"] == 0  # ni consulta el catálogo


# ─── PostFeedNode ──────────────────────────────────────────────────────────────
class FakeBsky:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def post(self, text, media_path=None):
        self.calls.append((text, media_path))
        return f"at://bot/{len(self.calls)}"


def _post_state(**over):
    base = {"should_post": True, "post_text": "miren esto", "feed_name": "f",
            "autonomous_images": False, "image_query": ""}
    base.update(over)
    return base


def test_postfeed_attaches_image_when_enabled(tmp_path, monkeypatch):
    conn = d.init_db(tmp_path / "p.db")
    monkeypatch.setattr(b, "resolve_catalog_image", lambda c, q, **k: "scrape/pictures/manual/x.jpg")
    bsky = FakeBsky()
    out = b.PostFeedNode(bsky, conn).run(_post_state(autonomous_images=True, image_query="gato"))
    assert out["posted_uri"] == "at://bot/1"
    assert bsky.calls[0][1] == "scrape/pictures/manual/x.jpg"


def test_postfeed_ignores_image_when_disabled(tmp_path, monkeypatch):
    conn = d.init_db(tmp_path / "p.db")
    spy = {"called": False}
    monkeypatch.setattr(b, "resolve_catalog_image",
                        lambda c, q, **k: spy.__setitem__("called", True) or "x.jpg")
    bsky = FakeBsky()
    # autonomous_images=False aunque venga image_query → nunca resuelve ni adjunta.
    b.PostFeedNode(bsky, conn).run(_post_state(autonomous_images=False, image_query="gato"))
    assert bsky.calls[0][1] is None
    assert spy["called"] is False


def test_postfeed_no_image_query_no_image(tmp_path, monkeypatch):
    conn = d.init_db(tmp_path / "p.db")
    bsky = FakeBsky()
    b.PostFeedNode(bsky, conn).run(_post_state(autonomous_images=True, image_query=""))
    assert bsky.calls[0][1] is None
