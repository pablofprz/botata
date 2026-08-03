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

# El id y el sufijo de la URL son EL MISMO string (como en Spotify real): el
# anti-repetición quema por id extraído de la URL posteada — si difieren acá,
# el filtro nunca matchea y el test queda a merced del random.choice.
_TRACKS = [
    {"id": "1", "title": "Flaca", "artist": "Andrés Calamaro",
     "url": "https://open.spotify.com/track/1"},
    {"id": "2", "title": "Zamba de mi esperanza", "artist": "Los Chalchaleros",
     "url": "https://open.spotify.com/track/2"},
]


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "share.db")


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [{"type": "spotify", "name": "comunitaria",
                                    "category": "", "sources": ["PL1"], "enabled": True}])
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
    monkeypatch.setattr(b, "SOURCES", [])
    out = _call(conn)
    assert "no hay playlist configurada" in out.text


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


# ─── T38b: varias playlists, elegidas por tema ───────────────────────────────
_POR_PLAYLIST = {
    "PL_ROCK":   [{"id": "r1", "title": "Rock del país", "artist": "Banda",
                   "url": "https://open.spotify.com/track/r1"}],
    "PL_CUMBIA": [{"id": "c1", "title": "Cumbia del sol", "artist": "Grupo",
                   "url": "https://open.spotify.com/track/c1"}],
}


@pytest.fixture()
def varias(monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "spotify", "name": "rockola", "category": "rock",
         "sources": ["PL_ROCK"], "enabled": True},
        {"type": "spotify", "name": "bailanta", "category": "cumbia",
         "sources": ["PL_CUMBIA"], "enabled": True},
    ])
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: list(_POR_PLAYLIST[pid]))


def test_topic_elige_la_playlist(conn, varias):
    out = b._tool_get_playlist_track({"topic": "rock"}, ToolContext(state={}, conn=conn))
    assert "Rock del país" in out.text and "Cumbia" not in out.text


def test_sin_topic_junta_todas(conn, varias):
    vistos = set()
    for _ in range(12):
        vistos.add(b._tool_get_playlist_track(
            {}, ToolContext(state={}, conn=conn)).text.split("\n")[0])
    assert len(vistos) == 2          # elige entre los temas de ambas playlists


def test_topic_sin_playlist_avisa_y_sugiere(conn, varias):
    out = b._tool_get_playlist_track({"topic": "jazz"}, ToolContext(state={}, conn=conn))
    assert "no tengo playlist para 'jazz'" in out.text
    assert "rock" in out.text and "cumbia" in out.text


def test_una_playlist_rota_no_tumba_al_resto(conn, monkeypatch, varias):
    def _falla(pid, tok):
        if pid == "PL_ROCK":
            raise RuntimeError("403")
        return list(_POR_PLAYLIST[pid])
    monkeypatch.setattr(b, "playlist_tracks", _falla)
    out = b._tool_get_playlist_track({}, ToolContext(state={}, conn=conn))
    assert "Cumbia del sol" in out.text
