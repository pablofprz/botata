"""Tests de la tool `get_my_recent_posts` (T25): el bot habla de su propia actividad."""
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


@pytest.fixture()
def conn(tmp_path):
    c = d.init_db(tmp_path / "act.db")
    for h in ("user1.bsky.social", "user2.bsky.social"):
        c.execute("INSERT INTO users(handle) VALUES (?)", (h,))
    c.commit()
    # tres posts del bot: dos replies (a distintos handles) y uno raíz.
    b.log_bot_post(c, uri="at://bot/1", in_reply_to="at://x/1",
                   reply_to_handle="user1.bsky.social", text="jaja obvio")
    b.log_bot_post(c, uri="at://bot/2", in_reply_to="at://x/2",
                   reply_to_handle="user2.bsky.social", text="no sé, fijate vos")
    b.log_bot_post(c, uri="at://bot/3", in_reply_to=None,
                   reply_to_handle=None, text="buen día comunidad")
    return c


def _ctx(conn):
    return ToolContext(state={"author_handle": "quien.sea"}, conn=conn)


def test_lists_recent_activity(conn):
    out = b._tool_get_my_recent_posts({}, _ctx(conn)).text
    assert "jaja obvio" in out
    assert "buen día comunidad" in out
    assert "(respuesta a @user1.bsky.social)" in out


def test_filter_by_handle(conn):
    out = b._tool_get_my_recent_posts({"handle": "@user2.bsky.social"}, _ctx(conn)).text
    assert "no sé, fijate vos" in out
    assert "jaja obvio" not in out  # ese fue a user1


def test_filter_no_match_is_graceful(conn):
    out = b._tool_get_my_recent_posts({"handle": "nadie.bsky.social"}, _ctx(conn)).text
    assert "no encontré" in out


def test_empty_history_is_graceful(tmp_path):
    c = d.init_db(tmp_path / "empty.db")
    out = b._tool_get_my_recent_posts({}, ToolContext(state={}, conn=c)).text
    assert "no tengo posts recientes" in out


def test_scopes_default(conn):
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    assert "get_my_recent_posts" in [t.name for t in reg.available(Scope.REPLY)]
    assert "get_my_recent_posts" in [t.name for t in reg.available(Scope.ADMIN)]
    assert "get_my_recent_posts" not in [t.name for t in reg.available(Scope.FEED_REFLECTION)]
