"""Tests de imágenes autónomas + guardrail (T12).

`hybrid_search_image_catalog` va mockeado (evita cargar bge-m3). El foco es el
guardrail de `resolve_catalog_image` y el cableo en `PostFeedNode`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
    assert b.resolve_catalog_image(None, "gato") == "scrape/pictures/manual/x.jpg"
    assert used == [1]


def test_guardrail_rejects_empty_description(monkeypatch, used):
    _mock_search(monkeypatch, [_row(description="  ")])
    assert b.resolve_catalog_image(None, "gato") is None
    assert used == []  # no la marca como usada


def test_guardrail_rejects_invalid_category(monkeypatch, used):
    _mock_search(monkeypatch, [_row(category="nsfw")])
    assert b.resolve_catalog_image(None, "gato") is None


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

    def post(self, text, image_path=None):
        self.calls.append((text, image_path))
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
