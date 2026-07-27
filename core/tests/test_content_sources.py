"""T38 · Registro de fuentes de contenido por tema (config/content_sources.json).

El tema NO lo adivina el modelo de visión: el admin declara qué fuente es de qué
tema y la búsqueda se acota a esas fuentes. Resolución en query (editar el
registro aplica en caliente, sin reindexar).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
import db as d  # noqa: E402
from tools import ToolContext  # noqa: E402

_SOURCES = [
    {"source": "futbol_memes_ok", "category": "futbol", "platform": "instagram", "enabled": True},
    {"source": "pasion_tribunera", "category": "futbol", "platform": "instagram", "enabled": True},
    {"source": "politica_shitpost", "category": "politica", "platform": "instagram", "enabled": True},
    {"source": "vieja_cuenta", "category": "futbol", "platform": "instagram", "enabled": False},
]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(b, "CONTENT_SOURCES", _SOURCES)


# ─── sources_for_topic ───────────────────────────────────────────────────────
def test_resuelve_tema_a_fuentes():
    assert b.sources_for_topic("futbol") == ["futbol_memes_ok", "pasion_tribunera"]


def test_ignora_fuentes_desactivadas():
    assert "vieja_cuenta" not in b.sources_for_topic("futbol")


def test_matcheo_tolerante_case_y_substring():
    # el tema lo escribe un LLM: mayúsculas y frases sueltas tienen que pegar
    assert b.sources_for_topic("FUTBOL") == b.sources_for_topic("futbol")
    assert b.sources_for_topic("politica") == ["politica_shitpost"]


def test_tema_desconocido_da_lista_vacia():
    assert b.sources_for_topic("cocina") == []
    assert b.sources_for_topic("") == []


# ─── filtro en la búsqueda del catálogo ──────────────────────────────────────
@pytest.fixture()
def catalogo(tmp_path, monkeypatch):
    """Catálogo con dos fuentes; embeddings dummy para no cargar bge-m3."""
    conn = d.init_db(tmp_path / "cat.db")
    monkeypatch.setattr(d, "embed", lambda text: b"\x00" * (4 * 1024))
    rows = [
        ("instagram", "a1", "futbol_memes_ok", "scrape/pictures/instagram/a1_0.jpg",
         "un jugador festejando un gol", "meme"),
        ("instagram", "b1", "politica_shitpost", "scrape/pictures/instagram/b1_0.jpg",
         "un politico en conferencia", "meme"),
    ]
    for platform, ext, src, path, desc, cat in rows:
        conn.execute(
            "INSERT INTO image_catalog (platform, external_id, source_name, file_path, "
            "description, category, tags) VALUES (?,?,?,?,?,?,'[]')",
            (platform, ext, src, path, desc, cat))
    conn.commit()
    return conn


def test_filtro_por_fuentes_acota_el_resultado(catalogo):
    # sin filtro, "politico" encuentra la imagen de la cuenta de política
    sin_filtro = d.hybrid_search_image_catalog(catalogo, "politico", limit=8)
    assert {r["source_name"] for r in sin_filtro} == {"politica_shitpost"}

    # con el filtro temático puesto en fútbol, esa misma búsqueda no devuelve nada:
    # el registro manda por encima de la relevancia semántica.
    con_filtro = d.hybrid_search_image_catalog(
        catalogo, "politico", sources=["futbol_memes_ok"], limit=8)
    assert con_filtro == []

    # y la búsqueda propia del tema sí resuelve dentro de su fuente
    futbol = d.hybrid_search_image_catalog(
        catalogo, "gol", sources=["futbol_memes_ok"], limit=8)
    assert {r["source_name"] for r in futbol} == {"futbol_memes_ok"}


def test_lista_de_fuentes_vacia_no_devuelve_nada(catalogo):
    # sources=[] significa "el tema existe pero no tiene fuentes": mejor nada que
    # ignorar el filtro y postear cualquier cosa.
    assert d.hybrid_search_image_catalog(catalogo, "gol", sources=[], limit=8) == []


def test_combina_con_el_filtro_de_categoria(catalogo):
    assert d.hybrid_search_image_catalog(
        catalogo, "gol", category="foto", sources=["futbol_memes_ok"], limit=8) == []
    assert d.hybrid_search_image_catalog(
        catalogo, "gol", category="meme", sources=["futbol_memes_ok"], limit=8)


# ─── la tool search_images ───────────────────────────────────────────────────
def test_tool_acota_por_topic(catalogo, monkeypatch):
    monkeypatch.setattr(b, "_postable_media_path", lambda p: "/abs/" + p)
    ctx = ToolContext(state={}, conn=catalogo)
    out = b._tool_search_images({"query": "gol", "topic": "futbol"}, ctx)
    assert "politico" not in out.text
    assert out.image_path and "a1_0.jpg" in out.image_path


def test_tool_topic_desconocido_avisa_y_no_postea(catalogo):
    ctx = ToolContext(state={}, conn=catalogo)
    out = b._tool_search_images({"query": "algo", "topic": "cocina"}, ctx)
    assert "no tengo fuentes declaradas para 'cocina'" in out.text
    assert "futbol" in out.text and "politica" in out.text   # sugiere los temas reales
    assert out.image_path is None


def test_tool_sin_topic_busca_en_todo(catalogo, monkeypatch):
    monkeypatch.setattr(b, "_postable_media_path", lambda p: "/abs/" + p)
    ctx = ToolContext(state={}, conn=catalogo)
    out = b._tool_search_images({"query": "politico"}, ctx)
    assert out.image_path is not None


def test_registro_vacio_no_rompe(catalogo, monkeypatch):
    monkeypatch.setattr(b, "CONTENT_SOURCES", [])
    ctx = ToolContext(state={}, conn=catalogo)
    out = b._tool_search_images({"query": "gol", "topic": "futbol"}, ctx)
    assert "el admin no cargó fuentes de contenido" in out.text
