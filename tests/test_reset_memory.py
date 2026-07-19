"""Tests de `resetme` (T11): purga la memoria del propio handle, sin tocar a otros.

Inserta embeddings dummy directo en user_facts_vec (evita cargar bge-m3) para
verificar que también se borran, no solo las filas de user_facts."""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

DIM = 1024


def _emb(v: float = 0.1) -> bytes:
    return struct.pack(f"<{DIM}f", *([v] * DIM))


def _seed_user(conn, handle: str, fact: str):
    conn.execute("INSERT INTO users(handle) VALUES (?)", (handle,))
    conn.execute("INSERT INTO user_facts(handle, fact_text) VALUES (?, ?)", (handle, fact))
    fid = conn.execute(
        "SELECT id FROM user_facts WHERE handle = ? ORDER BY id DESC LIMIT 1", (handle,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO user_facts_vec(rowid, embedding, partition_key) VALUES (?, ?, ?)",
        (fid, _emb(), handle),
    )
    d.create_event(conn, title="cumple", event_at="2026-08-01", handle=handle)
    conn.commit()
    return fid


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "reset.db")
    return c


def _vec_count(conn, rowid):
    return conn.execute("SELECT COUNT(*) FROM user_facts_vec WHERE rowid = ?", (rowid,)).fetchone()[0]


def test_purge_removes_facts_embeddings_events(conn):
    fid = _seed_user(conn, "u1.bsky.social", "vive en rosario")
    counts = d.purge_user_memory(conn, "u1.bsky.social")
    assert counts == {"facts": 1, "events": 1, "relationships": 0, "interactions": 0}
    assert conn.execute("SELECT COUNT(*) FROM user_facts WHERE handle='u1.bsky.social'").fetchone()[0] == 0
    assert _vec_count(conn, fid) == 0  # embedding también borrado
    assert conn.execute("SELECT COUNT(*) FROM events WHERE handle='u1.bsky.social'").fetchone()[0] == 0


def test_purge_isolates_other_users(conn):
    _seed_user(conn, "u1.bsky.social", "hincha de river")
    fid2 = _seed_user(conn, "u2.bsky.social", "toca la guitarra")
    d.purge_user_memory(conn, "u1.bsky.social")
    # u2 intacto: fact, embedding y evento siguen.
    assert conn.execute("SELECT COUNT(*) FROM user_facts WHERE handle='u2.bsky.social'").fetchone()[0] == 1
    assert _vec_count(conn, fid2) == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE handle='u2.bsky.social'").fetchone()[0] == 1


def test_resetme_keeps_profile_and_relationships(conn):
    _seed_user(conn, "u1.bsky.social", "x")
    _seed_user(conn, "u2.bsky.social", "y")
    conn.execute(
        "INSERT INTO relationships(handle_a, handle_b, kind) VALUES (?, ?, 'reply')",
        ("u1.bsky.social", "u2.bsky.social"),
    )
    conn.commit()
    d.purge_user_memory(conn, "u1.bsky.social")  # resetme: NO toca relationships ni users
    assert conn.execute("SELECT COUNT(*) FROM users WHERE handle='u1.bsky.social'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 1


def test_tool_resets_only_caller(conn):
    _seed_user(conn, "u1.bsky.social", "algo")
    ctx = ToolContext(state={"author_handle": "u1.bsky.social"}, conn=conn)
    res = b._tool_reset_my_memory({}, ctx)
    assert "borré" in res.text
    assert conn.execute("SELECT COUNT(*) FROM user_facts WHERE handle='u1.bsky.social'").fetchone()[0] == 0


def test_tool_without_author_is_graceful(conn):
    res = b._tool_reset_my_memory({}, ToolContext(state={}, conn=conn))
    assert "no pude identificar" in res.text


def test_scopes_default(conn):
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "reset_my_memory" in [t.name for t in reg.available(Scope.REPLY)]
    assert "reset_my_memory" in [t.name for t in reg.available(Scope.ADMIN)]
    assert "reset_my_memory" not in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
