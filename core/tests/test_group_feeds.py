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


def test_resolver_error_cachea_el_vacio(monkeypatch):
    """Regresión: una fuente rota (URI mal configurada, red caída) se reintentaba
    en CADA chequeo de permisos — 5.500 llamadas a la API en el log de arg. El
    vacío por error también se cachea un rato."""
    monkeypatch.setattr(b, "FEEDS_CONFIG", FEEDS)
    fake = FakeBsky()
    fake.fail = True
    resolve = b._make_group_feed_resolver(fake)
    resolve("polcifeed")
    resolve("polcifeed")
    assert fake.list_calls == 1  # la segunda sale del cache negativo, sin red


# ─── resolve_list_uri: URLs web de bsky.app ─────────────────────────────────

class _FakeProfile:
    did = "did:plc:abc123"


class _FakeXrpc:
    """Lo mínimo de self._client que resolve_list_uri toca."""
    class app:
        class bsky:
            class actor:
                @staticmethod
                def get_profile(params):
                    assert params["actor"] == "ppolci.com"
                    return _FakeProfile()


class _FakeClientSelf:
    _client = _FakeXrpc()
    _WEB_URI_RE = b.BskyClient._WEB_URI_RE
    _WEB_KIND = b.BskyClient._WEB_KIND


def _resolve(uri):
    return b.BskyClient.resolve_list_uri(_FakeClientSelf(), uri)


def test_resolve_list_uri_acepta_url_web_de_lista():
    """Regresión: la URL copiada del navegador (https://bsky.app/profile/...)
    se parseaba como at:// y "https:" terminaba de handle en getProfile —
    miles de errores y la fuente muerta en silencio."""
    out = _resolve("https://bsky.app/profile/ppolci.com/lists/3mi37g2byke27")
    assert out == "at://did:plc:abc123/app.bsky.graph.list/3mi37g2byke27"


def test_resolve_list_uri_acepta_url_web_de_feed():
    out = _resolve("https://bsky.app/profile/did:plc:abc123/feed/aaaa")
    assert out == "at://did:plc:abc123/app.bsky.feed.generator/aaaa"


def test_resolve_list_uri_at_did_pasa_intacta():
    uri = "at://did:plc:abc123/app.bsky.graph.list/1"
    assert _resolve(uri) == uri


def test_resolve_list_uri_formato_desconocido_no_inventa_handle():
    """Una URL que no es de bsky.app no debe llegar a getProfile: se devuelve
    intacta con warning (el fake explotaría si le llegara otro actor)."""
    uri = "https://ejemplo.com/lo-que-sea"
    assert _resolve(uri) == uri
