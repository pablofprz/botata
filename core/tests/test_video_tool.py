"""Tests de la tool share_video (T14, YouTube Data API). El HTTP a YouTube va mockeado."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
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
                        lambda c=None, q=None, topic=None: {"title": "V", "url": YT1, "channel": "Canal"})
    monkeypatch.setattr(b, "get_youtube_transcript", lambda u: "bla transcript")
    out = b._tool_share_video({}, ToolContext(state={}, conn=conn)).text
    assert "V" in out and YT1 in out and "Canal" in out and "transcript" in out


def test_tool_passes_query(monkeypatch, conn):
    seen = {}
    monkeypatch.setattr(b, "fetch_top_video",
                        lambda c=None, q=None, topic=None: seen.update(q=q) or {"title": "V", "url": YT1, "channel": "c"})
    monkeypatch.setattr(b, "get_youtube_transcript", lambda u: None)
    b._tool_share_video({"query": "cerati"}, ToolContext(state={}, conn=conn))
    assert seen["q"] == "cerati"


def test_tool_missing_key_is_graceful(monkeypatch, conn):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert "no están configurados" in b._tool_share_video({}, ToolContext(state={}, conn=conn)).text


def test_tool_no_video_is_graceful(monkeypatch, conn):
    monkeypatch.setattr(b, "fetch_top_video", lambda c=None, q=None, topic=None: None)
    assert "no encontré un video" in b._tool_share_video({}, ToolContext(state={}, conn=conn)).text


def test_tool_error_is_graceful(monkeypatch, conn):
    def _boom(c=None, q=None, topic=None):
        raise RuntimeError("youtube 403")
    monkeypatch.setattr(b, "fetch_top_video", _boom)
    assert "no pude traer un video" in b._tool_share_video({}, ToolContext(state={}, conn=conn)).text


def test_scopes_default(conn):
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    for sc in (Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN):
        assert "share_video" in [t.name for t in reg.available(sc)]


# ─── T38b: canales/listas de YouTube registrados por tema ────────────────────
def _pl_payload(*ids):
    return {"items": [
        {"snippet": {"title": f"video {i}", "resourceId": {"videoId": i},
                     "videoOwnerChannelTitle": "CanalReg"}} for i in ids]}


def test_traduce_canal_a_playlist_de_subidas():
    """UC… → UU…: la playlist de subidas del canal, sin gastar cuota de search."""
    assert b._youtube_uploads_playlist("UCabc123") == "UUabc123"
    assert b._youtube_uploads_playlist("PLxyz") == "PLxyz"   # ya es lista
    assert b._youtube_uploads_playlist("") is None


def test_resuelve_handle_y_cachea(monkeypatch):
    llamadas = []
    def _get(path, params):
        llamadas.append((path, params))
        return {"items": [{"id": "UCdelhandle"}]}
    monkeypatch.setattr(b, "_youtube_get", _get)
    monkeypatch.setattr(b, "_YT_HANDLE_CACHE", {})
    assert b._youtube_uploads_playlist("@canal") == "UUdelhandle"
    assert b._youtube_uploads_playlist("@canal") == "UUdelhandle"
    assert len(llamadas) == 1                     # la segunda sale del cache


def test_topic_saca_de_los_canales_registrados(conn, monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "youtube", "name": "rockolas", "category": "rock",
         "sources": ["UCrock"], "enabled": True}])
    vistos = []
    def _get(path, params):
        vistos.append(params.get("playlistId"))
        return _pl_payload("aaaaaaaaaaa")
    monkeypatch.setattr(b, "_youtube_get", _get)
    out = b._tool_share_video({"topic": "rock"}, ToolContext(state={}, conn=conn))
    assert vistos == ["UUrock"] and YT1 in out.text


def test_sin_nada_prefiere_los_registrados(conn, monkeypatch):
    """Si el admin registró canales, el default sale de ahí y no del mostPopular."""
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "youtube", "category": "comunidad", "sources": ["PLcom"], "enabled": True}])
    monkeypatch.setattr(b, "_youtube_get",
                        lambda path, params: _pl_payload("aaaaaaaaaaa"))
    monkeypatch.setattr(b, "youtube_top_videos",
                        lambda *a, **k: pytest.fail("no debería ir al mostPopular"))
    out = b._tool_share_video({}, ToolContext(state={}, conn=conn))
    assert YT1 in out.text


def test_la_busqueda_abierta_se_conserva(conn, monkeypatch):
    """Con `query` busca libre aunque haya canales registrados (el pedido del admin)."""
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "youtube", "category": "rock", "sources": ["UCrock"], "enabled": True}])
    monkeypatch.setattr(b, "youtube_top_videos",
                        lambda q=None, **k: [{"title": "buscado", "url": YT2, "channel": "C"}])
    out = b._tool_share_video({"query": "gatos"}, ToolContext(state={}, conn=conn))
    assert YT2 in out.text


def test_sin_registro_cae_en_lo_popular(conn, monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [])
    monkeypatch.setattr(b, "youtube_top_videos",
                        lambda q=None, **k: [{"title": "popular", "url": YT1, "channel": "C"}])
    out = b._tool_share_video({}, ToolContext(state={}, conn=conn))
    assert YT1 in out.text


def test_topic_desconocido_avisa(conn, monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "youtube", "category": "rock", "sources": ["UCrock"], "enabled": True}])
    out = b._tool_share_video({"topic": "cocina"}, ToolContext(state={}, conn=conn))
    assert "no tengo canales de YouTube para 'cocina'" in out.text
    assert "rock" in out.text


def test_una_fuente_rota_no_tumba_al_resto(monkeypatch):
    def _get(path, params):
        if params.get("playlistId") == "UUrota":
            raise RuntimeError("404")
        return _pl_payload("aaaaaaaaaaa")
    monkeypatch.setattr(b, "_youtube_get", _get)
    out = b.youtube_source_videos(["UCrota", "PLbuena"])
    assert [v["url"] for v in out] == [YT1]
