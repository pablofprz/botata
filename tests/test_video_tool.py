"""Tests de la tool share_video (T14, YouTube Data API). El HTTP a YouTube va mockeado."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

YT1 = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
YT2 = "https://www.youtube.com/watch?v=bbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "v.db")


# ─── youtube_top_videos: parseo de payloads de la API ─────────────────────────
def test_search_payload_parsed(monkeypatch):
    payload = {"items": [
        {"id": {"videoId": "aaaaaaaaaaa"},
         "snippet": {"title": "Cómo funciona X", "channelTitle": "CanalX"}},
        {"id": {"kind": "channel"}, "snippet": {"title": "no es video"}},  # sin videoId → se ignora
    ]}
    monkeypatch.setattr(b, "_youtube_get", lambda path, params: payload)
    out = b.youtube_top_videos(query="x")
    assert out == [{"title": "Cómo funciona X", "url": YT1, "channel": "CanalX"}]


def test_most_popular_payload_parsed(monkeypatch):
    payload = {"items": [{"id": "aaaaaaaaaaa",
                          "snippet": {"title": "Trend", "channelTitle": "Canal"}}]}
    monkeypatch.setattr(b, "_youtube_get", lambda path, params: payload)
    assert b.youtube_top_videos() == [{"title": "Trend", "url": YT1, "channel": "Canal"}]


def test_none_without_api_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert b.youtube_top_videos("x") is None


# ─── fetch_top_video: dedup contra bot_posts ──────────────────────────────────
def test_fetch_skips_already_posted(monkeypatch, conn):
    monkeypatch.setattr(b, "youtube_top_videos", lambda query=None: [
        {"title": "a", "url": YT1, "channel": "c"}, {"title": "b", "url": YT2, "channel": "c"}])
    b.log_bot_post(conn, uri="at://bot/1", in_reply_to=None, reply_to_handle=None,
                   text=f"ya compartí {YT1}")
    assert b.fetch_top_video(conn)["url"] == YT2


def test_fetch_none_when_empty(monkeypatch, conn):
    monkeypatch.setattr(b, "youtube_top_videos", lambda query=None: [])
    assert b.fetch_top_video(conn) is None


# ─── transcript best-effort ───────────────────────────────────────────────────
def test_transcript_invalid_url_is_none():
    assert b.get_youtube_transcript("https://example.com/no-video") is None


def test_transcript_missing_dep_is_none():
    assert b.get_youtube_transcript(YT1) is None  # youtube_transcript_api no instalado


# ─── tool ─────────────────────────────────────────────────────────────────────
def test_tool_formats_video(monkeypatch, conn):
    monkeypatch.setattr(b, "fetch_top_video",
                        lambda c=None, q=None: {"title": "V", "url": YT1, "channel": "Canal"})
    monkeypatch.setattr(b, "get_youtube_transcript", lambda u: "bla transcript")
    out = b._tool_share_video({}, ToolContext(state={}, conn=conn)).text
    assert "V" in out and YT1 in out and "Canal" in out and "transcript" in out


def test_tool_passes_query(monkeypatch, conn):
    seen = {}
    monkeypatch.setattr(b, "fetch_top_video",
                        lambda c=None, q=None: seen.update(q=q) or {"title": "V", "url": YT1, "channel": "c"})
    monkeypatch.setattr(b, "get_youtube_transcript", lambda u: None)
    b._tool_share_video({"query": "cerati"}, ToolContext(state={}, conn=conn))
    assert seen["q"] == "cerati"


def test_tool_missing_key_is_graceful(monkeypatch, conn):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert "no están configurados" in b._tool_share_video({}, ToolContext(state={}, conn=conn)).text


def test_tool_no_video_is_graceful(monkeypatch, conn):
    monkeypatch.setattr(b, "fetch_top_video", lambda c=None, q=None: None)
    assert "no encontré un video" in b._tool_share_video({}, ToolContext(state={}, conn=conn)).text


def test_tool_error_is_graceful(monkeypatch, conn):
    def _boom(c=None, q=None):
        raise RuntimeError("youtube 403")
    monkeypatch.setattr(b, "fetch_top_video", _boom)
    assert "no pude traer un video" in b._tool_share_video({}, ToolContext(state={}, conn=conn)).text


def test_scopes_default(conn):
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    for sc in (Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN):
        assert "share_video" in [t.name for t in reg.available(sc)]
