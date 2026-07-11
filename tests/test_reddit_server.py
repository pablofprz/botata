"""Tests del MCP server reddit (T18): parsing Atom, URLs, rate limiter, cache.

Sin red: `_http_get` se monkeypatchea. El E2E de spawn (server real por stdio
vía MCPBridge) valida el pipeline T29→T18 sin tocar Reddit.
"""
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mcp_servers"))

import reddit_server as rs

_ATOM_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>posts de prueba</title>
  <entry>
    <author><name>/u/alguien</name></author>
    <title>Primer post</title>
    <link href="https://www.reddit.com/r/test/comments/abc/primer_post/"/>
    <published>2026-07-11T10:00:00+00:00</published>
    <content type="html">&lt;p&gt;Texto del &lt;b&gt;post&lt;/b&gt; con    espacios&lt;/p&gt;</content>
  </entry>
  <entry>
    <author><name>/u/otro</name></author>
    <title>Segundo post</title>
    <link href="https://www.reddit.com/r/test/comments/def/segundo/"/>
    <updated>2026-07-11T11:00:00+00:00</updated>
  </entry>
</feed>"""


@pytest.fixture(autouse=True)
def _reset_state():
    rs._cache.clear()
    rs._last_request_at = 0.0
    yield


# ─── Parsing ─────────────────────────────────────────────────────────────────
def test_parse_atom_campos():
    posts = rs._parse_atom(_ATOM_FIXTURE, limit=10)
    assert len(posts) == 2
    assert posts[0]["title"] == "Primer post"
    assert posts[0]["author"] == "/u/alguien"
    assert posts[0]["url"].endswith("/primer_post/")
    assert posts[0]["published"] == "2026-07-11T10:00:00+00:00"
    assert posts[0]["summary"] == "Texto del post con espacios"   # HTML limpio
    assert posts[1]["published"] == "2026-07-11T11:00:00+00:00"   # cae a <updated>


def test_parse_atom_limit():
    assert len(rs._parse_atom(_ATOM_FIXTURE, limit=1)) == 1


# ─── URLs ────────────────────────────────────────────────────────────────────
def test_feed_urls():
    assert rs._feed_url("argentina", "hot", "week") == "https://www.reddit.com/r/argentina/.rss"
    assert rs._feed_url("r/argentina", "new", "week") == "https://www.reddit.com/r/argentina/new/.rss"
    assert rs._feed_url("a+b", "top", "day") == "https://www.reddit.com/r/a+b/top/.rss?t=day"


@pytest.mark.parametrize("sub,sort,tw", [
    ("arg entina", "hot", "week"),      # espacio
    ("../etc", "hot", "week"),          # path traversal
    ("argentina", "best", "week"),      # sort inválido
    ("argentina", "top", "decade"),     # window inválido
])
def test_feed_url_valida(sub, sort, tw):
    with pytest.raises(ValueError):
        rs._feed_url(sub, sort, tw)


# ─── Rate limiter + cache ────────────────────────────────────────────────────
def test_fetch_espacia_requests(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(rs, "_http_get", lambda url: calls.append(url) or _ATOM_FIXTURE)
    monkeypatch.setattr(rs.time, "sleep", lambda s: sleeps.append(s))
    rs._fetch("https://x/1")
    rs._fetch("https://x/2")            # dentro de la ventana → duerme hasta el slot
    assert len(calls) == 2
    assert len(sleeps) == 1 and 0 < sleeps[0] <= rs.MIN_INTERVAL_S


def test_fetch_cachea(monkeypatch):
    calls = []
    monkeypatch.setattr(rs, "_http_get", lambda url: calls.append(url) or _ATOM_FIXTURE)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    rs._fetch("https://x/1")
    rs._fetch("https://x/1")            # misma URL dentro del TTL → cache, sin GET
    assert len(calls) == 1


def test_tool_end_to_end_sin_red(monkeypatch):
    monkeypatch.setattr(rs, "_http_get", lambda url: _ATOM_FIXTURE)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    out = rs.get_subreddit_posts("argentina", sort="new", limit=1)
    assert '"Primer post"' in out and '"posts"' in out


# ─── E2E: spawn real por stdio vía el cliente MCP (sin red) ──────────────────
def test_e2e_spawn_y_list_tools():
    import mcp_tools
    from tools import ToolRegistry
    reg = ToolRegistry()
    bridge = mcp_tools.MCPBridge()
    try:
        cfg = {"reddit": {"transport": "stdio", "command": sys.executable,
                          "args": [str(_ROOT / "mcp_servers" / "reddit_server.py")]}}
        n = mcp_tools.register_mcp_tools(reg, cfg, bridge=bridge)
        assert n == 1
        assert reg.get("reddit_get_subreddit_posts") is not None
        assert reg.get("reddit_get_subreddit_posts").scopes == frozenset({"admin"})
    finally:
        bridge.shutdown()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
