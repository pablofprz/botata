"""`get_data`: la otra mitad del conector `api` generalizado.

Una comunidad no habla solo de fotos — el dólar, el clima, si hoy juega Boca.
Antes eso no tenía puerta: el tipo `api` exigía mapear una imagen, así que cada
dato nuevo era una tool a medida. Ahora es la MISMA fuente configurable, leída
por otra tool según lo que el admin haya mapeado.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import connectors as c  # noqa: E402
from tools import Scope, ToolContext  # noqa: E402

_DOLAR = {"type": "api", "name": "dólar", "category": "dolar",
          "sources": ["blue", "oficial"], "enabled": True,
          "url": "https://dolarapi.com/v1/dolares/{source}", "items_path": "",
          "map": {"title": "casa", "compra": "compra", "venta": "venta"}}
_MEMES = {"type": "api", "name": "memes", "category": "memes",
          "sources": ["meme"], "enabled": True,
          "url": "https://api.test/{source}", "items_path": "",
          "map": {"image_url": "img"}}


@pytest.fixture(autouse=True)
def fuentes(monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [_DOLAR, _MEMES])


@pytest.fixture()
def api_falsa(monkeypatch):
    """Reemplaza el fetch del conector: los tests no salen a la red."""
    llamadas = []

    def _fake(source, limit=15, entry=None):
        llamadas.append(source)
        return [{"image_url": "", "url": "", "title": source,
                 "fields": {"compra": 1410, "venta": 1435}, "source": source}]

    monkeypatch.setattr(c, "api_items", _fake)
    return llamadas


def _pedir(topic=""):
    return b._tool_get_data({"topic": topic}, ToolContext(state={}, conn=None)).text


def test_trae_los_datos_del_tema(api_falsa):
    texto = _pedir("dolar")
    assert "blue" in texto and "venta: 1435" in texto
    assert api_falsa == ["blue", "oficial"]      # una llamada por fuente


def test_no_toca_las_fuentes_de_imagen(api_falsa):
    """Las entradas que mapean imagen son de get_latest_media, no de acá."""
    _pedir("memes")
    assert api_falsa == []                        # no la consultó


def test_sin_tema_conocido_dice_cuales_tiene(api_falsa):
    texto = _pedir("cotizacion del yen")
    assert "no tengo datos" in texto and "dolar" in texto


def test_sin_fuentes_de_datos_lo_dice(monkeypatch, api_falsa):
    monkeypatch.setattr(b, "SOURCES", [_MEMES])
    assert "no tengo ninguna fuente de datos" in _pedir("dolar")


def test_una_fuente_caida_no_se_lleva_el_resto(monkeypatch):
    def _fake(source, limit=15, entry=None):
        if source == "blue":
            raise c.ApiSourceError("la API no respondió")
        return [{"image_url": "", "url": "", "title": source,
                 "fields": {"venta": 1010}, "source": source}]

    monkeypatch.setattr(c, "api_items", _fake)
    texto = _pedir("dolar")
    assert "oficial" in texto and "1010" in texto


def test_si_fallan_todas_lo_explica(monkeypatch):
    def _boom(source, limit=15, entry=None):
        raise c.ApiSourceError("la API no respondió")

    monkeypatch.setattr(c, "api_items", _boom)
    texto = _pedir("dolar")
    assert "no pude traer" in texto and "no respondió" in texto


def test_el_texto_es_plano_no_json(api_falsa):
    """Un JSON crudo en el prompt empuja al modelo a copiarlo tal cual."""
    texto = _pedir("dolar")
    assert "{" not in texto and "compra: 1410" in texto


# ─── que las dos mitades no se pisen ─────────────────────────────────────────
def test_get_latest_media_ignora_las_fuentes_de_datos():
    entradas = b._entradas_en_vivo("dolar")
    assert entradas == []                         # la del dólar no es de media


def test_get_latest_media_sigue_viendo_las_de_imagen():
    assert [e["name"] for _, e in b._entradas_en_vivo("memes")] == ["memes"]


def test_scopes():
    reg = b.build_tool_registry(b.TOOLS_CONFIG)
    disponibles = [t.name for t in reg.available(Scope.FEED_REFLECTION)]
    assert "get_data" in disponibles              # las rutinas la necesitan
    assert "get_data" in [t.name for t in reg.available(Scope.REPLY)]
