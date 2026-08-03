"""Tests de la tool search_music (T13). El HTTP a Spotify va mockeado."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_CTX = ToolContext(state={}, conn=None)
_TRACKS = [
    {"title": "Flaca", "artist": "Andrés Calamaro", "album": "Alta Suciedad",
     "url": "https://open.spotify.com/track/1"},
    {"title": "Cuando no estás", "artist": "Andrés Calamaro", "album": "Honestidad Brutal",
     "url": "https://open.spotify.com/track/2"},
]


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")


def test_returns_tracks_with_links(monkeypatch):
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: _TRACKS)
    out = b._tool_search_music({"query": "calamaro"}, _CTX).text
    assert "Flaca" in out and "Andrés Calamaro" in out
    assert "https://open.spotify.com/track/1" in out


def test_empty_query(monkeypatch):
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: _TRACKS)
    assert "necesito una canción" in b._tool_search_music({"query": "  "}, _CTX).text


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    assert "no está configurada" in b._tool_search_music({"query": "x"}, _CTX).text


def test_no_results(monkeypatch):
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: [])
    assert "no encontré temas" in b._tool_search_music({"query": "zzzzzz"}, _CTX).text


def test_error_is_graceful(monkeypatch):
    def _boom(q, limit=5):
        raise RuntimeError("spotify 500")
    monkeypatch.setattr(b, "search_spotify_tracks", _boom)
    assert "no pude buscar música" in b._tool_search_music({"query": "x"}, _CTX).text


def test_search_parses_spotify_payload(monkeypatch):
    # search_spotify_tracks arma los dicts desde el JSON crudo de /search.
    payload = {"tracks": {"items": [
        {"id": "t1", "name": "Flaca", "artists": [{"name": "Andrés Calamaro"}],
         "album": {"name": "Alta Suciedad"},
         "external_urls": {"spotify": "https://open.spotify.com/track/1"}},
    ]}}
    monkeypatch.setattr(b, "_spotify_token", lambda: "tok")
    monkeypatch.setattr(b, "_spotify_get", lambda path, token, params=None: payload)
    tracks = b.search_spotify_tracks("calamaro")
    assert tracks == [{"id": "t1", "title": "Flaca", "artist": "Andrés Calamaro",
                       "album": "Alta Suciedad", "url": "https://open.spotify.com/track/1"}]


def test_search_returns_none_without_token(monkeypatch):
    monkeypatch.setattr(b, "_spotify_token", lambda: None)
    assert b.search_spotify_tracks("x") is None


def test_scopes_default():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    for sc in (Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN):
        assert "search_music" in [t.name for t in reg.available(sc)]
