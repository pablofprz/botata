"""Tests del resolver de membresía dinámica de grupos (USER_GROUPS feed:)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import botata as b


class FakeBsky:
    def __init__(self):
        self.list_calls = 0
        self.members = ["fulano.bsky.social", "Mengano.com"]
        self.fail = False

    def get_list_members(self, uri):
        self.list_calls += 1
        if self.fail:
            raise RuntimeError("red caída")
        return list(self.members)

    def get_follows(self):
        return ["seguido.bsky.social"]


FEEDS = [
    {"name": "polcifeed", "type": "list", "uri": "at://x/app.bsky.graph.list/1"},
    {"name": "timeline", "type": "following"},
    {"name": "algo", "type": "feed", "uri": "at://x/app.bsky.feed.generator/1"},
]


def test_resolver_list_y_cache(monkeypatch):
    monkeypatch.setattr(b, "FEEDS_CONFIG", FEEDS)
    fake = FakeBsky()
    resolve = b._make_group_feed_resolver(fake)
    assert resolve("polcifeed") == frozenset({"fulano.bsky.social", "Mengano.com"})
    resolve("polcifeed")
    assert fake.list_calls == 1  # segunda llamada sale del cache (TTL 15 min)


def test_resolver_stale_ok(monkeypatch):
    monkeypatch.setattr(b, "FEEDS_CONFIG", FEEDS)
    monkeypatch.setattr(b, "_GROUP_FEED_TTL_S", -1)  # expira al instante
    fake = FakeBsky()
    resolve = b._make_group_feed_resolver(fake)
    ok = resolve("polcifeed")
    fake.fail = True
    # red caída → sirve el último resultado bueno, no vacío (un hiccup no saca permisos)
    assert resolve("polcifeed") == ok


def test_resolver_following_y_tipos_invalidos(monkeypatch):
    monkeypatch.setattr(b, "FEEDS_CONFIG", FEEDS)
    resolve = b._make_group_feed_resolver(FakeBsky())
    assert resolve("timeline") == frozenset({"seguido.bsky.social"})
    assert resolve("algo") == frozenset()        # feed algorítmico: sin membresía
    assert resolve("fantasma") == frozenset()    # feed inexistente: cerrado


def test_resolver_error_sin_cache_es_cerrado(monkeypatch):
    monkeypatch.setattr(b, "FEEDS_CONFIG", FEEDS)
    fake = FakeBsky()
    fake.fail = True
    resolve = b._make_group_feed_resolver(fake)
    assert resolve("polcifeed") == frozenset()
