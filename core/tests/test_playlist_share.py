"""Tests de playlist_share: postear temas de la playlist comunitaria con comentario."""
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

_TRACKS = [
    {"id": "t1", "title": "Flaca", "artist": "Andrés Calamaro",
     "url": "https://open.spotify.com/track/1"},
    {"id": "t2", "title": "Zamba de mi esperanza", "artist": "Los Chalchaleros",
     "url": "https://open.spotify.com/track/2"},
]


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "share.db")


class FakeBsky:
    def __init__(self):
        self.posts = []

    def post(self, text, media_path=None):
        self.posts.append(text)
        return f"at://fake/{len(self.posts)}"


class FakeLLM:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def complete(self, system, user, model_cls=None):
        self.calls += 1
        self.last_user = user
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setattr(b, "SPOTIFY_PLAYLIST_ID", "PL1")
    monkeypatch.setattr(sa, "user_token", lambda: "user-tok")
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: list(_TRACKS))
    monkeypatch.setattr(b, "TASKS_CONFIG", {"playlist_share": {"comment": True}})


def _run(conn, bsky, llm, monkeypatch):
    monkeypatch.setattr(b, "RoleLLM", lambda router, role: llm)
    b.run_playlist_share_pass(bsky, router=None, conn=conn)


def test_postea_con_comentario_y_link(conn, monkeypatch):
    llm = FakeLLM(b.FeedDecision(should_post=True, reason="ok", text="temazo de la lista"))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert len(bsky.posts) == 1
    assert bsky.posts[0].startswith("temazo de la lista\n")
    assert "open.spotify.com/track/" in bsky.posts[0]     # el link se agrega solo
    assert "—" in llm.last_user                            # el track llegó al LLM
    # quedó registrado en bot_posts
    assert conn.execute("SELECT COUNT(*) FROM bot_posts").fetchone()[0] == 1


def test_sin_comentario_no_llama_llm(conn, monkeypatch):
    monkeypatch.setattr(b, "TASKS_CONFIG", {"playlist_share": {"comment": False}})
    llm, bsky = FakeLLM(b.FeedDecision(should_post=True, text="no debería usarse")), FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert llm.calls == 0
    assert len(bsky.posts) == 1
    assert "open.spotify.com/track/" in bsky.posts[0]
    assert "—" in bsky.posts[0]                            # título — artista


def test_anti_repeticion_elige_fresco(conn, monkeypatch):
    # el track 1 ya fue posteado hace poco → debe elegir el 2
    b.log_bot_post(conn, uri="at://bot/1", in_reply_to=None, reply_to_handle=None,
                   text="mirá esto https://open.spotify.com/track/1")
    monkeypatch.setattr(b, "TASKS_CONFIG", {"playlist_share": {"comment": False}})
    bsky = FakeBsky()
    _run(conn, bsky, FakeLLM(None), monkeypatch)
    assert "track/2" in bsky.posts[0] and "track/1" not in bsky.posts[0]


def test_llm_roto_degrada_a_sin_comentario(conn, monkeypatch):
    llm, bsky = FakeLLM(RuntimeError("llm caído")), FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert len(bsky.posts) == 1                            # el share es la función
    assert "open.spotify.com/track/" in bsky.posts[0]


def test_agente_declina_no_postea(conn, monkeypatch):
    llm = FakeLLM(b.FeedDecision(should_post=False, reason="datos rotos", text=""))
    bsky = FakeBsky()
    _run(conn, bsky, llm, monkeypatch)
    assert bsky.posts == []


def test_sin_token_skip_silencioso(conn, monkeypatch):
    monkeypatch.setattr(sa, "user_token", lambda: None)
    bsky = FakeBsky()
    _run(conn, bsky, FakeLLM(None), monkeypatch)
    assert bsky.posts == []


def test_playlist_vacia_skip(conn, monkeypatch):
    monkeypatch.setattr(b, "playlist_tracks", lambda pid, tok: [])
    bsky = FakeBsky()
    _run(conn, bsky, FakeLLM(None), monkeypatch)
    assert bsky.posts == []
