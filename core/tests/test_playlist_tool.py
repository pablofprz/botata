"""Tests de get_playlist_track: la pata de DATOS de la rutina de playlist
(la conducta — compartir y comentar — vive en routines/playlist.md)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b
import db as d
import spotify_auth as sa
from tools import ToolContext

_TRACKS = [
    {"id": "t1", "title": "Flaca", "artist": "Andrés Calamaro",
     "url": "https://open.spotify.com/track/1"},
    {"id": "t2", "title": "Zamba de mi esperanza", "artist": "Los Chalchaleros",
     "url": "https://open.spotify.com/track/2"},
]


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "share.db")


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setattr(b, "SPOTIFY_PLAYLIST_ID", "PL1")
    monkeypatch.setattr(sa, "user_token", lambda: "user-tok")
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: list(_TRACKS))


def _call(conn):
    return b._tool_get_playlist_track({}, ToolContext(state={}, conn=conn))


def test_trae_tema_con_titulo_artista_y_link(conn):
    out = _call(conn)
    assert "—" in out.text                              # título — artista
    assert "open.spotify.com/track/" in out.text        # link a incluir
    assert "playlist comunitaria" in out.text


def test_anti_repeticion_elige_fresco(conn):
    # el track 1 ya fue posteado hace poco → debe traer el 2
    b.log_bot_post(conn, uri="at://bot/1", in_reply_to=None, reply_to_handle=None,
                   text="mirá esto https://open.spotify.com/track/1")
    out = _call(conn)
    assert "track/2" in out.text and "track/1" not in out.text


def test_todos_recientes_elige_igual(conn):
    """Playlist chica con todo reciente: elige al azar igual (no se traba)."""
    for i in (1, 2):
        b.log_bot_post(conn, uri=f"at://bot/{i}", in_reply_to=None,
                       reply_to_handle=None,
                       text=f"tema https://open.spotify.com/track/{i}")
    out = _call(conn)
    assert "open.spotify.com/track/" in out.text


def test_sin_token_mensaje_claro(conn, monkeypatch):
    monkeypatch.setattr(sa, "user_token", lambda: None)
    out = _call(conn)
    assert "spotify_auth" in out.text


def test_sin_playlist_configurada(conn, monkeypatch):
    monkeypatch.setattr(b, "SPOTIFY_PLAYLIST_ID", "")
    out = _call(conn)
    assert "SPOTIFY_PLAYLIST_ID" in out.text


def test_playlist_vacia(conn, monkeypatch):
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: [])
    out = _call(conn)
    assert "vacía" in out.text


def test_api_rota_no_lanza(conn, monkeypatch):
    def _boom(pid, tok):
        raise RuntimeError("spotify caído")
    monkeypatch.setattr(b, "playlist_tracks", _boom)
    out = _call(conn)
    assert "no pude leer" in out.text


def test_registrada_con_scope_feed_reflection():
    """La rutina de playlist necesita la tool en su fase de tools."""
    reg = b.build_tool_registry()
    tool = reg.get("get_playlist_track")
    assert tool is not None
    assert "feed_reflection" in tool.scopes and "admin" in tool.scopes
    # y get_my_recent_posts quedó disponible para rutinas reflexivas
    assert "feed_reflection" in reg.get("get_my_recent_posts").scopes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
