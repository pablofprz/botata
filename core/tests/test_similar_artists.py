"""Tests de `similar_artists` (MusicBrainz + ListenBrainz Labs). Sin red.

Por qué existe la tool: a la app le quedó de Spotify SOLO `/search`
(`related-artists` 403, `/recommendations` 404, `audio-features` 403), así que
"algo parecido a X" no puede salir de Spotify por más que se mejore search_music.

Lo que más importa acá es la auto-reparación del algoritmo: el endpoint de
similares es de ListenBrainz *Labs* (experimental) y su enum YA cambió una vez —
la string que andaba el 2026-08-01 devolvía 400 el 08-02.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_MBID = "956301d0-4b3c-4ffc-9898-b39305d25112"
_ARTISTA = {"artists": [{"id": _MBID, "name": "Patricio Rey y sus Redonditos de Ricota",
                         "disambiguation": "", "country": "AR"}]}
_SIMILARES = [{"artist_mbid": "x1", "name": "Charly García", "score": 219},
              {"artist_mbid": "x2", "name": "Divididos", "score": 200},
              {"artist_mbid": "x3", "name": "Sumo", "score": 180}]


@pytest.fixture(autouse=True)
def _sin_pausa(monkeypatch):
    """El ritmo de 1 req/s es real contra la API, pero no hay que pagarlo acá."""
    monkeypatch.setattr(b.time, "sleep", lambda s: None)


def _fake_get(mapa):
    """_mb_get falso: devuelve según qué URL le pidan."""
    def get(url):
        for clave, valor in mapa.items():
            if clave in url:
                if isinstance(valor, Exception):
                    raise valor
                return valor
        raise AssertionError(f"URL inesperada: {url}")
    return get


def _ctx(conn=None):
    return ToolContext(state={}, conn=conn)


def test_camino_feliz(monkeypatch):
    monkeypatch.setattr(b, "_mb_get", _fake_get(
        {"musicbrainz.org": _ARTISTA, "similar-artists": _SIMILARES}))
    out = b._tool_similar_artists({"artist": "Los Redondos"}, _ctx()).text
    assert "Patricio Rey y sus Redonditos de Ricota (AR)" in out   # resolvió el apodo
    assert "Charly García, Divididos, Sumo" in out
    assert "search_music" in out            # el resultado empuja a encadenar


def test_respeta_el_limite(monkeypatch):
    monkeypatch.setattr(b, "_mb_get", _fake_get(
        {"musicbrainz.org": _ARTISTA, "similar-artists": _SIMILARES}))
    out = b._tool_similar_artists({"artist": "x", "limit": 2}, _ctx()).text
    assert "Charly García, Divididos." in out and "Sumo" not in out


def test_artista_inexistente(monkeypatch):
    monkeypatch.setattr(b, "_mb_get", _fake_get({"musicbrainz.org": {"artists": []}}))
    assert "no encontré" in b._tool_similar_artists({"artist": "asdkjh"}, _ctx()).text


def test_sin_artista():
    assert "necesito un artista" in b._tool_similar_artists({"artist": " "}, _ctx()).text


def test_artista_sin_parecidos(monkeypatch):
    monkeypatch.setattr(b, "_mb_get", _fake_get(
        {"musicbrainz.org": _ARTISTA, "similar-artists": []}))
    out = b._tool_similar_artists({"artist": "x"}, _ctx()).text
    assert "no tengo parecidos" in out


def test_musicbrainz_caido_no_rompe(monkeypatch):
    monkeypatch.setattr(b, "_mb_get", _fake_get(
        {"musicbrainz.org": RuntimeError("503")}))
    assert "no encontré" in b._tool_similar_artists({"artist": "x"}, _ctx()).text


# ─── Auto-reparación del algoritmo ───────────────────────────────────────────
def _http400(cuerpo: str):
    return urllib.error.HTTPError("u", 400, "Bad Request", {}, BytesIO(cuerpo.encode()))


_ERROR_ENUM = ("1 validation error for SimilarArtistsViewerInput<br>algorithm<br>"
               "  value is not a valid enumeration member; permitted: "
               "&#39;session_based_days_9000_session_300_limit_50_skip_30&#39;")


def test_saca_los_algoritmos_del_error():
    assert b._algoritmos_permitidos(_ERROR_ENUM) == \
        ["session_based_days_9000_session_300_limit_50_skip_30"]


def test_si_el_algoritmo_cambio_reintenta_con_el_nuevo(monkeypatch):
    """Caso real: la string del 08-01 devolvía 400 el 08-02. El error trae las
    válidas, así que se reintenta en vez de dar la tool por muerta."""
    vistos = []

    def get(url):
        if "musicbrainz.org" in url:
            return _ARTISTA
        vistos.append(url)
        if len(vistos) == 1:
            raise _http400(_ERROR_ENUM)
        return _SIMILARES

    monkeypatch.setattr(b, "_mb_get", get)
    out = b._tool_similar_artists({"artist": "Los Redondos"}, _ctx()).text
    assert "Charly García" in out
    assert "session_based_days_9000_session_300_limit_50_skip_30" in vistos[1]


def test_400_sin_alternativas_no_cicla(monkeypatch):
    llamadas = []

    def get(url):
        if "musicbrainz.org" in url:
            return _ARTISTA
        llamadas.append(url)
        raise _http400("otra cosa cualquiera")

    monkeypatch.setattr(b, "_mb_get", get)
    assert "no tengo parecidos" in b._tool_similar_artists({"artist": "x"}, _ctx()).text
    assert len(llamadas) == 1


def test_otro_http_no_reintenta(monkeypatch):
    llamadas = []

    def get(url):
        if "musicbrainz.org" in url:
            return _ARTISTA
        llamadas.append(url)
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)

    monkeypatch.setattr(b, "_mb_get", get)
    assert "no tengo parecidos" in b._tool_similar_artists({"artist": "x"}, _ctx()).text
    assert len(llamadas) == 1


# ─── Cache: MusicBrainz pide ~1 req/s y los vecinos no cambian por día ───────
def test_la_segunda_vez_no_toca_la_red(tmp_path, monkeypatch):
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "cache.db")
    llamadas = []

    def get(url):
        llamadas.append(url)
        return _ARTISTA if "musicbrainz.org" in url else _SIMILARES

    monkeypatch.setattr(b, "_mb_get", get)
    primero = b._tool_similar_artists({"artist": "Los Redondos"}, _ctx(conn)).text
    n = len(llamadas)
    segundo = b._tool_similar_artists({"artist": "Los Redondos"}, _ctx(conn)).text
    assert segundo == primero and len(llamadas) == n      # 0 requests nuevas


def test_el_cache_vence(tmp_path, monkeypatch):
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "viejo.db")
    viejo = (b.now_local() - b.timedelta(days=b._MB_CACHE_DIAS + 1)).isoformat()
    dbmod.kv_set(conn, "mbz:artista:x", json.dumps({"cuando": viejo, "dato": {"mbid": "z"}}))
    assert b._cache_leer(conn, "mbz:artista:x") is None


def test_cache_corrupto_no_rompe(tmp_path):
    import db as dbmod
    conn = dbmod.init_db(tmp_path / "roto.db")
    dbmod.kv_set(conn, "mbz:artista:x", "esto no es json")
    assert b._cache_leer(conn, "mbz:artista:x") is None


def test_sin_conexion_a_la_db_anda_igual(monkeypatch):
    """El cache es una optimización, no un requisito."""
    monkeypatch.setattr(b, "_mb_get", _fake_get(
        {"musicbrainz.org": _ARTISTA, "similar-artists": _SIMILARES}))
    assert "Charly García" in b._tool_similar_artists({"artist": "x"}, _ctx(None)).text


def test_scopes_y_registro():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    for sc in (Scope.REPLY, Scope.FEED_REFLECTION, Scope.ADMIN):
        assert "similar_artists" in [t.name for t in reg.available(sc)]
