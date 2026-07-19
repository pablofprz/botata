"""Tests del dispatcher de fuentes de feed (T7): list | feed | following."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402


# ─── Fakes del SDK atproto ───────────────────────────────────────────────────
class _Record:
    def __init__(self, text):
        self.text = text


class _Author:
    def __init__(self, handle):
        self.handle = handle


class _Post:
    def __init__(self, handle, text, uri, indexed_at):
        self.author = _Author(handle)
        self.record = _Record(text)
        self.uri = uri
        self.indexed_at = indexed_at


class _Item:
    def __init__(self, post):
        self.post = post


class _Resp:
    def __init__(self, items, cursor=None):
        self.feed = items
        self.cursor = cursor


class FakeSDK:
    """Registra qué endpoint se llamó y con qué params; devuelve un post fijo."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        ts = datetime.now(timezone.utc).isoformat()
        self._resp = _Resp([_Item(_Post("u1", "hola feed", "at://p/1", ts))])
        outer = self

        class _Feed:
            def get_list_feed(self, params):
                outer.calls.append(("list", params)); return outer._resp

            def get_feed(self, params):
                outer.calls.append(("feed", params)); return outer._resp

            def get_timeline(self, params):
                outer.calls.append(("timeline", params)); return outer._resp

        class _Bsky:
            feed = _Feed()

        class _App:
            bsky = _Bsky()

        self.app = _App()


def _client(sdk):
    """BskyClient sin login (evita la red): inyecta el SDK falso."""
    c = b.BskyClient.__new__(b.BskyClient)
    c._client = sdk
    return c


def test_list_source_calls_get_list_feed():
    sdk = FakeSDK()
    c = _client(sdk)
    posts = c.get_feed_posts("list", "at://did:plc:x/app.bsky.graph.list/1", since=None)
    assert [name for name, _ in sdk.calls] == ["list"]
    assert sdk.calls[0][1]["list"] == "at://did:plc:x/app.bsky.graph.list/1"
    assert posts[0]["handle"] == "u1" and posts[0]["text"] == "hola feed"


def test_feed_source_calls_get_feed():
    sdk = FakeSDK()
    c = _client(sdk)
    c.get_feed_posts("feed", "at://did:plc:x/app.bsky.feed.generator/whats-hot", since=None)
    assert sdk.calls[0][0] == "feed"
    assert "feed" in sdk.calls[0][1]


def test_following_source_calls_get_timeline():
    sdk = FakeSDK()
    c = _client(sdk)
    c.get_feed_posts("following", None, since=None)
    assert sdk.calls[0][0] == "timeline"
    # timeline no lleva identificador
    assert "list" not in sdk.calls[0][1] and "feed" not in sdk.calls[0][1]


def test_unknown_type_falls_back_to_list():
    sdk = FakeSDK()
    c = _client(sdk)
    c.get_feed_posts("wat", "at://did:plc:x/app.bsky.graph.list/1", since=None)
    assert sdk.calls[0][0] == "list"


def test_since_filter_stops_at_old_posts():
    sdk = FakeSDK()
    # el post fijo es "ahora"; con since en el futuro, debe descartarse
    c = _client(sdk)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    posts = c.get_feed_posts("following", None, since=future)
    assert posts == []
