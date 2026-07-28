"""Conector declarativo `api`: describir una API JSON por config, sin código.

La razón de que exista: el que instale Botata no necesariamente programa. Un
`connectors/*.py` es barato para el dev y es un muro para todos los demás — este
conector mueve esa frontera a un formulario. De ahí que lo que más se testea acá
no sea el camino feliz sino los MENSAJES DE ERROR: son la única herramienta de
diagnóstico que tiene alguien que no puede leer un traceback.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import connectors as c  # noqa: E402

# Forma real de la PokéAPI: /pokemon/{nombre} devuelve UN objeto, no una lista.
_POKEAPI = {
    "name": "pikachu",
    "sprites": {"other": {"official-artwork": {
        "front_default": "https://raw.githubusercontent.com/x/pikachu.png"}}},
    "species": {"url": "https://pokeapi.co/api/v2/pokemon-species/25/"},
}

_ENTRY_POKE = {
    "type": "api", "category": "pokemon", "sources": ["pikachu"],
    "url": "https://pokeapi.co/api/v2/pokemon/{source}",
    "items_path": "",
    "map": {"image_url": "sprites.other.official-artwork.front_default",
            "title": "name", "url": "species.url"},
}


class _Resp:
    def __init__(self, data: bytes): self._d = data
    def read(self, n=None): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _stub(monkeypatch, payload, capturadas: list | None = None):
    def _open(req, timeout=15):
        if capturadas is not None:
            capturadas.append(req)
        cuerpo = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return _Resp(cuerpo.encode() if isinstance(cuerpo, str) else cuerpo)
    monkeypatch.setattr(c.urllib.request, "urlopen", _open)


# ─── dig(): elegir un campo con un path de puntos ────────────────────────────
def test_dig_camina_objetos_listas_e_indices():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert c.dig(data, "a.b.1.c") == 2
    assert c.dig(data, "") is data              # path vacío = la respuesta entera
    assert c.dig(data, "a.z") is None
    assert c.dig(data, "a.b.9.c") is None       # índice fuera de rango, sin explotar
    assert c.dig(data, "a.b.c") is None         # nombre donde hay una lista


def test_dig_tolera_guiones_en_las_claves():
    # `official-artwork` de la PokéAPI: el separador es el punto, no el guión.
    assert c.dig({"official-artwork": {"x": 1}}, "official-artwork.x") == 1


# ─── El camino feliz: la PokéAPI con un objeto único ─────────────────────────
def test_objeto_unico_se_trata_como_un_item(monkeypatch):
    _stub(monkeypatch, _POKEAPI)
    items = c.api_items("pikachu", 5, _ENTRY_POKE)
    assert items == [{
        "image_url": "https://raw.githubusercontent.com/x/pikachu.png",
        "url": "https://pokeapi.co/api/v2/pokemon-species/25/",
        "title": "pikachu", "source": "pikachu"}]


def test_lista_respeta_items_path_y_el_limite(monkeypatch):
    _stub(monkeypatch, {"results": [{"img": f"https://x/{i}.png"} for i in range(10)]})
    items = c.api_items("lo que sea", 3,
                        {"url": "https://api.test/x", "items_path": "results",
                         "map": {"image_url": "img"}})
    assert len(items) == 3


def test_saltea_items_sin_imagen_usable(monkeypatch):
    _stub(monkeypatch, {"r": [{"img": None}, {"img": "no-es-url"},
                              {"img": "https://x/ok.png"}]})
    items = c.api_items("x", 5, {"url": "https://api.test/x", "items_path": "r",
                                 "map": {"image_url": "img"}})
    assert [i["image_url"] for i in items] == ["https://x/ok.png"]


# ─── Plantillas: {source}, {limit}, {env:CLAVE} ──────────────────────────────
def test_interpola_source_y_limit_escapando_la_fuente(monkeypatch):
    reqs = []
    _stub(monkeypatch, _POKEAPI, reqs)
    c.api_items("mr mime", 7, {**_ENTRY_POKE,
                               "url": "https://api.test/p/{source}?n={limit}"})
    # El espacio va escapado: la fuente la escribe un humano en un campo de texto.
    assert reqs[0].full_url == "https://api.test/p/mr%20mime?n=7"


def test_interpola_credenciales_en_url_y_headers(monkeypatch):
    monkeypatch.setenv("MI_API_KEY", "s3cr3t")
    reqs = []
    _stub(monkeypatch, _POKEAPI, reqs)
    c.api_items("pikachu", 5, {
        **_ENTRY_POKE, "url": "https://api.test/x?key={env:MI_API_KEY}",
        "headers": {"Authorization": "Bearer {env:MI_API_KEY}"}})
    assert reqs[0].full_url == "https://api.test/x?key=s3cr3t"
    assert reqs[0].get_header("Authorization") == "Bearer s3cr3t"


def test_credencial_faltante_dice_cual(monkeypatch):
    monkeypatch.delenv("NO_EXISTE", raising=False)
    with pytest.raises(c.ApiSourceError, match="NO_EXISTE"):
        c.api_items("x", 5, {**_ENTRY_POKE, "url": "https://api.test/x?k={env:NO_EXISTE}"})


def test_entry_env_vars_lista_lo_que_pide_la_entrada():
    assert c.entry_env_vars({
        "url": "https://api.test/x?k={env:UNA}",
        "headers": {"Authorization": "Bearer {env:OTRA}"}}) == ("UNA", "OTRA")


# ─── Errores explicables: lo que ve el admin en la UI ────────────────────────
def test_sin_url_avisa():
    with pytest.raises(c.ApiSourceError, match="no tiene URL"):
        c.api_items("x", 5, {"map": {"image_url": "img"}})


def test_url_no_http_se_rechaza():
    # Un file:// o un esquema raro no llega a abrirse.
    with pytest.raises(c.ApiSourceError, match="http"):
        c.api_items("x", 5, {"url": "file:///etc/passwd", "map": {"image_url": "i"}})


def test_sin_campo_de_imagen_avisa(monkeypatch):
    _stub(monkeypatch, _POKEAPI)
    with pytest.raises(c.ApiSourceError, match="imagen"):
        c.api_items("x", 5, {"url": "https://api.test/x", "map": {}})


def test_respuesta_no_json_avisa(monkeypatch):
    _stub(monkeypatch, b"<html>no soy json</html>")
    with pytest.raises(c.ApiSourceError, match="no es JSON"):
        c.api_items("x", 5, _ENTRY_POKE)


def test_items_path_inexistente_nombra_el_path(monkeypatch):
    _stub(monkeypatch, {"otra_cosa": []})
    with pytest.raises(c.ApiSourceError, match="results"):
        c.api_items("x", 5, {"url": "https://api.test/x", "items_path": "results",
                             "map": {"image_url": "img"}})


def test_campo_de_imagen_equivocado_lo_dice(monkeypatch):
    _stub(monkeypatch, _POKEAPI)
    with pytest.raises(c.ApiSourceError, match="sprites.mal"):
        c.api_items("x", 5, {**_ENTRY_POKE, "map": {"image_url": "sprites.mal"}})


def test_api_caida_no_explota(monkeypatch):
    def _boom(req, timeout=15):
        raise OSError("connection refused")
    monkeypatch.setattr(c.urllib.request, "urlopen", _boom)
    with pytest.raises(c.ApiSourceError, match="no respondió"):
        c.api_items("x", 5, _ENTRY_POKE)


# ─── El fetcher registrado nunca propaga: una fuente rota no tumba un pase ───
def test_fetch_items_devuelve_vacio_ante_una_fuente_rota(monkeypatch):
    _stub(monkeypatch, b"no json")
    assert c.fetch_items("api", "x", entry=_ENTRY_POKE) == []


def test_fetch_items_le_pasa_la_entrada_solo_a_quien_la_pide():
    vistos = {}

    def viejo(source, limit=15):                      # conector clásico
        vistos["viejo"] = source
        return [{"image_url": "https://x/a.png"}]

    def nuevo(source, limit=15, entry=None):          # declarativo
        vistos["nuevo"] = entry
        return [{"image_url": "https://x/b.png"}]

    c.register_fetcher("_viejo", viejo)
    c.register_fetcher("_nuevo", nuevo)
    try:
        assert c.fetch_items("_viejo", "s", entry={"a": 1})   # no explota
        c.fetch_items("_nuevo", "s", entry={"a": 1})
        assert vistos["viejo"] == "s"
        assert vistos["nuevo"] == {"a": 1}
    finally:
        c._FETCHERS.pop("_viejo", None)
        c._FETCHERS.pop("_nuevo", None)


def test_fetch_items_de_un_conector_inexistente_es_vacio():
    assert c.fetch_items("no_existe", "x") == []


# ─── El conector está en el catálogo y es "en vivo" ──────────────────────────
def test_api_esta_en_el_catalogo_y_lo_consume_get_latest_media():
    con = c.by_id("api")
    assert con is not None and con.live and con.tool == "get_latest_media"
    assert not con.core                       # se puede apagar desde Plugins
