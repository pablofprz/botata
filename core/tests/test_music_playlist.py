"""Tests de add_music_recommendation (playlist comunitaria) y spotify_auth."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b
import spotify_auth as sa
from tools import ToolContext

_CTX = ToolContext(state={}, conn=None)
_TRACK = {"id": "t1", "title": "Flaca", "artist": "Andrés Calamaro",
          "album": "Alta Suciedad", "url": "https://open.spotify.com/track/1"}


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setattr(b, "SPOTIFY_PLAYLIST_ID", "PL1")


@pytest.fixture
def _con_token(monkeypatch):
    monkeypatch.setattr(sa, "user_token", lambda: "user-tok")


# ─── add_track_to_playlist ───────────────────────────────────────────────────
def test_agrega_track_nuevo(monkeypatch, _con_token):
    posted = {}
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=1: [_TRACK])
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: [
        {"id": "otro", "title": "Otro tema", "artist": "Otra banda", "url": "u"}])
    monkeypatch.setattr(b, "_spotify_post", lambda path, tok, payload: posted.update(
        {"path": path, "payload": payload}) or {})
    out = b.add_track_to_playlist("flaca calamaro")
    assert out["status"] == "added" and out["track"] == _TRACK
    assert posted["path"] == "/playlists/PL1/items"   # /tracks fue removido (feb-2026)
    assert posted["payload"] == {"uris": ["spotify:track:t1"]}


def test_duplicado_no_postea(monkeypatch, _con_token):
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=1: [_TRACK])
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: [dict(_TRACK)])
    monkeypatch.setattr(b, "_spotify_post",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no debía postear")))
    assert b.add_track_to_playlist("flaca")["status"] == "duplicate"


def test_duplicado_por_cancion_con_otro_id(monkeypatch, _con_token):
    # mismo tema en otra edición: ID distinto, título con sufijo de remaster,
    # artista principal igual (con invitado) → sigue siendo duplicado
    en_lista = {"id": "zzz", "title": "Flaca - Remastered 2019",
                "artist": "Andrés Calamaro, Invitado X", "url": "u"}
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=1: [_TRACK])
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: [en_lista])
    monkeypatch.setattr(b, "_spotify_post",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no debía postear")))
    assert b.add_track_to_playlist("flaca")["status"] == "duplicate"


def test_sin_resultados(monkeypatch, _con_token):
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=1: [])
    assert b.add_track_to_playlist("zzz")["status"] == "not_found"


def test_sin_token_es_unavailable(monkeypatch):
    monkeypatch.setattr(sa, "user_token", lambda: None)
    assert b.add_track_to_playlist("flaca")["status"] == "unavailable"


def test_sin_playlist_config(monkeypatch, _con_token):
    monkeypatch.setattr(b, "SPOTIFY_PLAYLIST_ID", "")
    assert b.add_track_to_playlist("flaca")["status"] == "unavailable"


# ─── tool handler ────────────────────────────────────────────────────────────
def test_tool_agrega_y_confirma(monkeypatch):
    monkeypatch.setattr(b, "add_track_to_playlist",
                        lambda q: {"status": "added", "track": _TRACK})
    out = b._tool_add_music({"query": "flaca"}, _CTX).text
    assert "agregué" in out and "Flaca" in out


def test_tool_duplicado(monkeypatch):
    monkeypatch.setattr(b, "add_track_to_playlist",
                        lambda q: {"status": "duplicate", "track": _TRACK})
    assert "ya estaba" in b._tool_add_music({"query": "flaca"}, _CTX).text


def test_tool_query_vacia():
    assert "necesito" in b._tool_add_music({"query": " "}, _CTX).text


def test_tool_error_graceful(monkeypatch):
    def boom(q):
        raise RuntimeError("spotify 500")
    monkeypatch.setattr(b, "add_track_to_playlist", boom)
    assert "probá más tarde" in b._tool_add_music({"query": "x"}, _CTX).text


def test_tool_sin_autorizar(monkeypatch):
    monkeypatch.setattr(b, "add_track_to_playlist",
                        lambda q: {"status": "unavailable", "reason": "sin token"})
    assert "no está configurada" in b._tool_add_music({"query": "x"}, _CTX).text


# ─── dedup: parseo de la playlist paginada ───────────────────────────────────
def test_playlist_track_ids_pagina(monkeypatch):
    # wrapper nuevo "item" (feb-2026) + uno con el viejo "track" (tolerancia)
    pages = [
        {"items": [{"item": {"id": "a"}}, {"item": {"id": "b"}}], "total": 3},
        {"items": [{"track": {"id": "c"}}, {"item": None}], "total": 3},
    ]
    calls = []
    def fake_get(path, token, params=None):
        assert path.endswith("/items")                # /tracks fue removido
        calls.append(params["offset"])
        return pages[len(calls) - 1]
    monkeypatch.setattr(b, "_spotify_get", fake_get)
    assert b.playlist_track_ids("PL1", "tok") == {"a", "b", "c"}
    assert calls == [0, 2]


# ─── spotify_auth: refresh headless ──────────────────────────────────────────
def test_user_token_refresca_y_cachea(monkeypatch, tmp_path):
    cache = tmp_path / ".spotify_cache"
    cache.write_text(json.dumps({"refresh_token": "rt-viejo"}), encoding="utf-8")
    monkeypatch.setattr(sa, "CACHE_PATH", cache)
    monkeypatch.setattr(sa, "_token_cache", {"value": None, "exp": 0.0})
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "sec")
    calls = []
    monkeypatch.setattr(sa, "_token_request", lambda data: calls.append(data) or
                        {"access_token": "at1", "expires_in": 3600, "refresh_token": "rt-nuevo"})
    assert sa.user_token() == "at1"
    assert calls[0]["grant_type"] == "refresh_token"
    # rotación del refresh token persistida
    assert json.loads(cache.read_text(encoding="utf-8"))["refresh_token"] == "rt-nuevo"
    # segunda llamada: sale del cache en memoria, sin request
    assert sa.user_token() == "at1" and len(calls) == 1


def test_user_token_sin_cache_es_none(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "CACHE_PATH", tmp_path / "nope")
    monkeypatch.setattr(sa, "_token_cache", {"value": None, "exp": 0.0})
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "sec")
    assert sa.user_token() is None
