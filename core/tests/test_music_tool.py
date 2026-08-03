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


# ─── No repetir temas cuando el bot elige solo (2026-08-03) ──────────────────
# La rutina `canciones` posteó Callejeros dos veces en tres pases: con el mismo
# humor arma casi la misma query, y Spotify contesta siempre lo mismo. En uno de
# los pases el ÚNICO resultado fue el tema ya posteado, así que ningún prompt lo
# podía evitar — no había otra cosa para elegir.

import db as dbmod  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = dbmod.init_db(tmp_path / "botata.db")
    yield c
    c.close()


def _posteado(conn, texto: str) -> None:
    conn.execute("INSERT INTO bot_posts (uri, text) VALUES (?, ?)", (texto[:20], texto))
    conn.commit()


_CON_ID = [
    {"id": "1", "title": "Creo", "artist": "Callejeros", "album": "a",
     "url": "https://open.spotify.com/track/1"},
    {"id": "2", "title": "Rock del Gato", "artist": "Ratones Paranoicos", "album": "b",
     "url": "https://open.spotify.com/track/2"},
]


def test_iniciativa_propia_saltea_lo_ya_compartido(monkeypatch, conn):
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: _CON_ID)
    _posteado(conn, "hoy ando nostálgico https://open.spotify.com/track/1")
    ctx = ToolContext(state={}, conn=conn, scope=Scope.FEED_REFLECTION)
    out = b._tool_search_music({"query": "rock nacional"}, ctx).text
    assert "Creo" not in out
    assert "Rock del Gato" in out


def test_contestando_a_alguien_no_filtra_nada(monkeypatch, conn):
    """Si te piden ESE tema, es ese tema: el filtro es solo para lo que el bot
    elige por su cuenta."""
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: _CON_ID)
    _posteado(conn, "hoy ando nostálgico https://open.spotify.com/track/1")
    ctx = ToolContext(state={}, conn=conn, scope=Scope.REPLY)
    assert "Creo" in b._tool_search_music({"query": "creo callejeros"}, ctx).text


def test_si_todo_esta_quemado_lo_dice_en_vez_de_ofrecerlo(monkeypatch, conn):
    """El caso exacto del log: un solo resultado, y ya posteado. Devolver la lista
    igual era pedirle al modelo que eligiera entre una opción."""
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: _CON_ID[:1])
    _posteado(conn, "hoy ando nostálgico https://open.spotify.com/track/1")
    ctx = ToolContext(state={}, conn=conn, scope=Scope.FEED_REFLECTION)
    out = b._tool_search_music({"query": "rock nacional"}, ctx).text
    assert "ya los compartiste" in out
    assert "open.spotify.com" not in out          # ni de casualidad un link para copiar


def test_busca_mas_hondo_cuando_va_a_filtrar(monkeypatch, conn):
    """Pedir 5 y filtrar deja al modelo sin candidatos; hay que pedir de más."""
    pedidos = []
    monkeypatch.setattr(b, "search_spotify_tracks",
                        lambda q, limit=5, artist=None: pedidos.append(limit) or _CON_ID)
    b._tool_search_music({"query": "x"}, ToolContext(state={}, conn=conn,
                                                    scope=Scope.FEED_REFLECTION))
    b._tool_search_music({"query": "x"}, ToolContext(state={}, conn=conn, scope=Scope.REPLY))
    assert pedidos == [20, 5]


def test_no_devuelve_el_mismo_tema_repetido(monkeypatch, conn):
    """Spotify manda el mismo tema con varios ids (single, álbum, recopilado) y
    llenaba la lista de repetidos: el modelo veía 5 opciones y eran 2."""
    dobles = [
        {"id": "a", "title": "Jaff - Round 1", "artist": "Klan", "album": "x",
         "url": "https://open.spotify.com/track/a"},
        {"id": "b", "title": "Jaff - Round 1", "artist": "Klan", "album": "y",
         "url": "https://open.spotify.com/track/b"},
        {"id": "c", "title": "Otra", "artist": "Klan", "album": "z",
         "url": "https://open.spotify.com/track/c"},
    ]
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: dobles)
    out = b._tool_search_music({"query": "klan"}, ToolContext(state={}, conn=conn)).text
    assert out.count("Jaff - Round 1") == 1
    assert "Otra" in out


def test_la_memoria_de_temas_sale_de_lo_posteado(conn):
    _posteado(conn, "escuchate esto https://open.spotify.com/track/abc123 tremendo")
    _posteado(conn, "un post sin música")
    assert b._temas_ya_compartidos(conn) == {"abc123"}


def test_sin_db_no_se_rompe(monkeypatch):
    monkeypatch.setattr(b, "search_spotify_tracks", lambda q, limit=5, artist=None: _CON_ID)
    ctx = ToolContext(state={}, conn=None, scope=Scope.FEED_REFLECTION)
    assert "Creo" in b._tool_search_music({"query": "x"}, ctx).text
