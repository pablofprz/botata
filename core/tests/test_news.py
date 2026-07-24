"""Tests del pipeline de noticias RSS (T15). El fetch HTTP y el LLM van mockeados."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402


# ─── fetch_rss: parseo RSS 2.0 real (stdlib) ──────────────────────────────────
class _FakeResp:
    def __init__(self, data): self._data = data
    def read(self): return self._data
    def __enter__(self): return self
    def __exit__(self, *a): return False


_RSS = (
    b"<rss><channel>"
    b"<item><title>Titular 1</title><link>http://n/1</link>"
    b"<description>&lt;p&gt;bajada uno&lt;/p&gt;</description></item>"
    b"<item><title>Titular 2</title><link>http://n/2</link>"
    b"<description>bajada dos</description></item>"
    b"</channel></rss>"
)


def test_fetch_rss_parses_and_strips_html(monkeypatch):
    monkeypatch.setattr(b.urllib.request, "urlopen", lambda req, timeout=15: _FakeResp(_RSS))
    items = b.fetch_rss("http://feed")
    assert len(items) == 2
    assert items[0] == {"title": "Titular 1", "link": "http://n/1",
                        "description": "bajada uno", "id": "http://n/1"}


# ─── run_news_pass ────────────────────────────────────────────────────────────
class FakeBsky:
    def __init__(self): self.posts: list[str] = []
    def post(self, text, media_path=None):
        self.posts.append(text)
        return f"at://bot/{len(self.posts)}"


def _items(*ns):
    return [{"title": f"T{n}", "link": f"http://n/{n}", "description": f"d{n}", "id": f"http://n/{n}"}
            for n in ns]


def _src(mode="comment", **over):
    base = {"url": "http://f", "host": "f", "title": "F", "mode": mode,
            "enabled": True, "interval_hours": 6}
    base.update(over)
    return base


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "news.db")


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(b, "NEWS_ENABLED", True)
    monkeypatch.setattr(b, "NEWS_SOURCES", [_src()])


def test_master_toggle_off_does_nothing(conn, monkeypatch):
    monkeypatch.setattr(b, "NEWS_ENABLED", False)
    bsky = FakeBsky()
    b.run_news_pass(bsky, None, conn, force=True)
    assert bsky.posts == []


def test_comment_mode_posts_once_and_marks_all(conn, monkeypatch):
    monkeypatch.setattr(b, "fetch_rss", lambda url, max_items=15: _items(1, 2))
    monkeypatch.setattr(b, "_summarize_news", lambda router, items, src: "qué momento el país")
    bsky = FakeBsky()
    b.run_news_pass(bsky, None, conn, force=True)
    assert bsky.posts == ["qué momento el país"]        # un solo comentario
    assert d.news_item_posted(conn, "http://n/1") and d.news_item_posted(conn, "http://n/2")


def test_dedup_second_pass_no_new(conn, monkeypatch):
    monkeypatch.setattr(b, "fetch_rss", lambda url, max_items=15: _items(1, 2))
    monkeypatch.setattr(b, "_summarize_news", lambda router, items, src: "comentario")
    bsky = FakeBsky()
    b.run_news_pass(bsky, None, conn, force=True)
    b.run_news_pass(bsky, None, conn, force=True)  # mismos items → nada nuevo
    assert len(bsky.posts) == 1


def test_post_mode_posts_each_capped(conn, monkeypatch):
    monkeypatch.setattr(b, "NEWS_SOURCES", [_src(mode="post")])
    monkeypatch.setattr(b, "fetch_rss", lambda url, max_items=15: _items(1, 2, 3, 4, 5))
    bsky = FakeBsky()
    b.run_news_pass(bsky, None, conn, force=True, max_post_items=2)
    assert len(bsky.posts) == 2                         # capado a 2 por pasada
    assert "T1" in bsky.posts[0] and "http://n/1" in bsky.posts[0]
    # los no posteados siguen 'nuevos'
    assert not d.news_item_posted(conn, "http://n/3")


def test_only_new_items_posted(conn, monkeypatch):
    d.mark_news_item_posted(conn, "http://n/1", "f", "T1")  # 1 ya posteado
    captured = {}
    monkeypatch.setattr(b, "fetch_rss", lambda url, max_items=15: _items(1, 2))
    monkeypatch.setattr(b, "_summarize_news",
                        lambda router, items, src: captured.update(n=[i["id"] for i in items]) or "c")
    b.run_news_pass(FakeBsky(), None, conn, force=True)
    assert captured["n"] == ["http://n/2"]              # solo el nuevo llega al LLM


def test_interval_respected(conn, monkeypatch):
    monkeypatch.setattr(b, "fetch_rss", lambda url, max_items=15: _items(1))
    monkeypatch.setattr(b, "_summarize_news", lambda router, items, src: "c")
    bsky = FakeBsky()
    b.run_news_pass(bsky, None, conn, force=True)          # setea el cursor
    b.run_news_pass(bsky, None, conn, respect_interval=True)  # dentro del intervalo → skip
    assert len(bsky.posts) == 1
