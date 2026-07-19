"""Tests de `blockme` (T10). El block de Bluesky va MOCKEADO (no se ejecuta en vivo).

Cubre: purga total con drop_profile, el fix de FK de bot_posts.reply_to_handle,
que un block fallido no borra nada, aislamiento y scopes."""
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


def _seed(conn, handle, fact):
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


class FakeBsky:
    def __init__(self, ok=True):
        self.ok = ok
        self.blocked: list[str] = []

    def block_user(self, handle):
        self.blocked.append(handle)
        return self.ok


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "block.db")


def test_block_me_blocks_and_purges_everything(conn):
    fid = _seed(conn, "u1.bsky.social", "vive en cordoba")
    conn.execute("INSERT INTO users(handle) VALUES ('partner.bsky.social')")
    conn.execute(
        "INSERT INTO relationships(handle_a, handle_b, kind) "
        "VALUES ('u1.bsky.social','partner.bsky.social','reply')")
    conn.commit()
    bsky = FakeBsky(ok=True)
    res = b._make_block_me_tool(bsky)({}, ToolContext(state={"author_handle": "u1.bsky.social"}, conn=conn))
    assert bsky.blocked == ["u1.bsky.social"]
    assert "te bloqueé" in res.text
    # todo borrado: fact, embedding, evento, relación, perfil
    assert conn.execute("SELECT COUNT(*) FROM user_facts WHERE handle='u1.bsky.social'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_facts_vec WHERE rowid=?", (fid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE handle='u1.bsky.social'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM users WHERE handle='u1.bsky.social'").fetchone()[0] == 0


def test_drop_profile_nulls_bot_posts_reference(conn):
    # bot_posts.reply_to_handle → users(handle) es NO ACTION: sin el fix, borrar el
    # perfil violaría el FK. Verifica que el post sobrevive con la referencia en NULL.
    _seed(conn, "u1.bsky.social", "algo")
    b.log_bot_post(conn, uri="at://bot/1", in_reply_to="at://x/1",
                   reply_to_handle="u1.bsky.social", text="te respondí esto")
    d.purge_user_memory(conn, "u1.bsky.social", include_relationships=True, drop_profile=True)
    row = conn.execute("SELECT reply_to_handle, text FROM bot_posts WHERE uri='at://bot/1'").fetchone()
    assert row is not None                      # el post del bot NO se borra
    assert row["reply_to_handle"] is None       # pero la referencia quedó suelta


def test_failed_block_purges_nothing(conn):
    _seed(conn, "u1.bsky.social", "no me borres")
    bsky = FakeBsky(ok=False)
    res = b._make_block_me_tool(bsky)({}, ToolContext(state={"author_handle": "u1.bsky.social"}, conn=conn))
    assert "no pude bloquearte" in res.text
    assert conn.execute("SELECT COUNT(*) FROM user_facts WHERE handle='u1.bsky.social'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM users WHERE handle='u1.bsky.social'").fetchone()[0] == 1


def test_block_me_isolates_others(conn):
    _seed(conn, "u1.bsky.social", "a")
    _seed(conn, "u2.bsky.social", "b")
    b._make_block_me_tool(FakeBsky(ok=True))({}, ToolContext(state={"author_handle": "u1.bsky.social"}, conn=conn))
    assert conn.execute("SELECT COUNT(*) FROM users WHERE handle='u2.bsky.social'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM user_facts WHERE handle='u2.bsky.social'").fetchone()[0] == 1


def test_no_author_is_graceful(conn):
    res = b._make_block_me_tool(FakeBsky())({}, ToolContext(state={}, conn=conn))
    assert "no pude identificar" in res.text


def test_scopes_default(conn):
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "block_me" in [t.name for t in reg.available(Scope.REPLY)]
    assert "block_me" in [t.name for t in reg.available(Scope.ADMIN)]
    assert "block_me" not in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
