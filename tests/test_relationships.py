"""Tests del grafo de relaciones: bump_relationship + hooks mecánicos.

Sin LLM: las aristas se acumulan de forma puramente mecánica (thread de
menciones y replies del feed), solo entre usuarios ya conocidos.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402

U1 = "user1.bsky.social"
U2 = "user2.bsky.social"
U3 = "unknown.bsky.social"  # sin fila en users


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "rel.db")
    for h in (U1, U2):
        c.execute("INSERT INTO users(handle) VALUES (?)", (h,))
    c.commit()
    return c


def _edge(conn):
    return conn.execute("SELECT * FROM relationships").fetchone()


# ─── db.bump_relationship ────────────────────────────────────────────────────
def test_bump_crea_y_acumula_no_dirigida(conn):
    assert d.bump_relationship(conn, U1, U2, kind="thread")
    assert d.bump_relationship(conn, U2, U1, kind="thread")  # orden inverso = misma arista
    e = _edge(conn)
    assert (e["handle_a"], e["handle_b"]) == tuple(sorted((U1, U2)))
    assert e["weight"] == 2.0
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 1


def test_bump_kinds_distintos_aristas_distintas(conn):
    d.bump_relationship(conn, U1, U2, kind="thread")
    d.bump_relationship(conn, U1, U2, kind="reply")
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 2


def test_bump_ignora_desconocidos_y_self(conn):
    assert not d.bump_relationship(conn, U1, U3)   # U3 no tiene perfil
    assert not d.bump_relationship(conn, U1, U1)   # self-edge
    assert _edge(conn) is None


# ─── hook de menciones: participantes del thread ─────────────────────────────
def test_thread_participants_parsea_handles():
    # handles con forma de dominio, sin duplicados; texto sin forma de handle se filtra
    ctx = f"{U2}: hola que tal\nbot.bsky.social: buenas\n{U2}: te repito\nno es handle: x"
    assert b._thread_participants(ctx) == [U2, "bot.bsky.social"]


def test_bump_thread_relationships(conn, monkeypatch):
    monkeypatch.setattr(b, "BSKY_HANDLE", "bot.bsky.social")
    ctx = f"{U2}: arrancó el hilo\nbot.bsky.social: respondí yo\n{U1}: y yo me metí"
    b._bump_thread_relationships(conn, U1, ctx)
    e = _edge(conn)
    assert e is not None and e["kind"] == "thread"
    assert (e["handle_a"], e["handle_b"]) == tuple(sorted((U1, U2)))
    # ni el bot ni el self-edge crearon aristas extra
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 1


def test_bump_thread_jamas_lanza(conn):
    b._bump_thread_relationships(None, U1, "lo que sea")  # conn rota → loguea, no explota


# ─── hook del feed: autor ↔ respondido ───────────────────────────────────────
def test_fetch_feed_bumpea_replies(conn, monkeypatch):
    posts = [
        {"handle": U1, "text": "jaja", "uri": "at://1", "reply_to": U2},
        {"handle": U1, "text": "root", "uri": "at://2", "reply_to": None},
        {"handle": U1, "text": "al bot", "uri": "at://3", "reply_to": b.BSKY_HANDLE},
    ]

    class FakeBsky:
        def get_feed_posts(self, *a, **k):
            return posts

    monkeypatch.setattr(b, "get_feed_last_run", lambda c, n: None)
    monkeypatch.setattr(b, "save_feed_last_run", lambda c, n: None)
    node = b.FetchFeedNode(FakeBsky(), conn)
    out = node.run({"feed_name": "f", "feed_type": "list", "list_uri": "x"})
    assert out["posts_count"] == 3
    e = _edge(conn)
    assert e is not None and e["kind"] == "reply" and e["weight"] == 1.0
