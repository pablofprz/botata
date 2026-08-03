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

# Un TEMA, muchas fuentes (la forma que guarda la UI).
_SOURCES = [
    {"category": "futbol", "sources": ["futbol_memes_ok", "pasion_tribunera"], "enabled": True},
    {"category": "politica", "sources": ["politica_shitpost"], "enabled": True},
    {"category": "humor viejo", "sources": ["vieja_cuenta"], "enabled": False},
]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [{**e, "type": "membrilla"} for e in _SOURCES])


# ─── loader: normaliza las formas que puede tener el archivo ─────────────────
def _load(tmp_path, monkeypatch, raw):
    import json
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "content_sources.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(b, "CONFIG_DIR", cfg)
    return b._load_sources()


def test_loader_forma_nueva(tmp_path, monkeypatch):
    out = _load(tmp_path, monkeypatch,
                [{"category": "futbol", "sources": ["a", "b"]}])
    assert out[0]["sources"] == ["a", "b"]


def test_loader_acepta_forma_vieja_de_una_fuente(tmp_path, monkeypatch):
    out = _load(tmp_path, monkeypatch, [{"category": "futbol", "source": "a"}])
    assert out[0]["sources"] == ["a"]


def test_loader_acepta_string_con_comas(tmp_path, monkeypatch):
    """Por si alguien edita el JSON a mano y escribe las fuentes en una línea."""
    out = _load(tmp_path, monkeypatch, [{"category": "futbol", "sources": "a, b ,c"}])
    assert out[0]["sources"] == ["a", "b", "c"]


def test_loader_descarta_entradas_sin_fuentes(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, [{"category": "futbol", "sources": []}]) == []


# ─── sources_for_topic ───────────────────────────────────────────────────────
def test_resuelve_tema_a_fuentes():
    assert b.sources_for_topic("futbol") == ["futbol_memes_ok", "pasion_tribunera"]


def test_una_fuente_puede_estar_en_varios_temas(monkeypatch):
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "membrilla", "category": "futbol",
         "sources": ["compartida", "solo_futbol"], "enabled": True},
        {"type": "membrilla", "category": "humor", "sources": ["compartida"], "enabled": True},
    ])
    assert b.sources_for_topic("futbol") == ["compartida", "solo_futbol"]
    assert b.sources_for_topic("humor") == ["compartida"]


def test_ignora_temas_desactivados():
    assert b.sources_for_topic("humor viejo") == []


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
    monkeypatch.setattr(b, "SOURCES", [])
    ctx = ToolContext(state={}, conn=catalogo)
    out = b._tool_search_images({"query": "gol", "topic": "futbol"}, ctx)
    assert "el admin no cargó fuentes de contenido" in out.text


# ─── La playlist suelta de settings es SOLO migración ───────────────────────
def _load_con_playlist(tmp_path, monkeypatch, sources_json):
    """Instancia con SPOTIFY_PLAYLIST_ID en settings; `sources_json=None` = todavía
    sin registro (instancia vieja)."""
    import json
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    if sources_json is not None:
        (cfg / "sources.json").write_text(json.dumps(sources_json), encoding="utf-8")
    monkeypatch.setattr(b, "CONFIG_DIR", cfg)
    monkeypatch.setattr(b, "settings", {**b.settings, "SPOTIFY_PLAYLIST_ID": "AJENA"})
    return [e for e in b._load_sources() if e["type"] == "spotify"]


def test_sin_registro_la_playlist_de_settings_se_migra(tmp_path, monkeypatch):
    """Instancia que nunca abrió la UI: sin esto se quedaría sin playlist."""
    sp = _load_con_playlist(tmp_path, monkeypatch, None)
    assert [s for e in sp for s in e["sources"]] == ["AJENA"]


def test_con_registro_manda_el_registro_y_no_settings(tmp_path, monkeypatch):
    """El bug de botata-rancher (2026-08-01): heredó el SPOTIFY_PLAYLIST_ID de la
    instancia de la que se clonó, el motor se lo sumaba a su propia playlist y
    terminó leyendo Y ESCRIBIENDO en la playlist de la otra comunidad. La UI
    muestra sources.json, así que esa fuente era invisible e imborrable."""
    sp = _load_con_playlist(tmp_path, monkeypatch,
                            [{"type": "spotify", "name": "la mía", "sources": ["PROPIA"]}])
    assert [s for e in sp for s in e["sources"]] == ["PROPIA"]


# ─── YouTube: lo que pega un humano vs lo que acepta la API ─────────────────
import youtube_auth as ya  # noqa: E402


@pytest.mark.parametrize("pegado, esperado", [
    ("https://www.youtube.com/playlist?list=PLL5QXRk91MYY&jct=-SLfRFPeLgR3", "PLL5QXRk91MYY"),
    ("https://www.youtube.com/watch?v=abc&list=PLxyz", "PLxyz"),
    ("https://www.youtube.com/channel/UCabc123", "UCabc123"),
    ("https://www.youtube.com/@lanacion", "@lanacion"),
    ("PLL5QXRk91MYY", "PLL5QXRk91MYY"),          # id pelado: no se toca
    ("@lanacion", "@lanacion"),
])
def test_la_fuente_de_youtube_se_normaliza_a_id(pegado, esperado):
    """El bug de 2026-08-01: la URL entera viajaba como id de playlist y la API
    devolvía 400, así que la fuente quedaba muda sin decir por qué."""
    assert ya.source_id(pegado) == esperado


@pytest.mark.parametrize("link, esperado", [
    ("https://www.youtube.com/watch?v=-HJwbxVe8Rg", "-HJwbxVe8Rg"),
    ("https://youtu.be/-HJwbxVe8Rg?si=abc", "-HJwbxVe8Rg"),
    ("https://www.youtube.com/shorts/-HJwbxVe8Rg", "-HJwbxVe8Rg"),
    ("-HJwbxVe8Rg", "-HJwbxVe8Rg"),
    ("https://www.youtube.com/playlist?list=PLxyz", None),   # una lista no es un video
    ("cualquier cosa", None),
])
def test_el_video_se_saca_de_cualquier_forma_de_link(link, esperado):
    assert ya.video_id(link) == esperado


def test_el_id_de_playlist_llega_a_la_api(tmp_path, monkeypatch):
    """La fuente registrada como URL tiene que resolver igual que el id pelado."""
    assert b._youtube_uploads_playlist(
        "https://www.youtube.com/playlist?list=PLL5QXRk91MYY&jct=x") == "PLL5QXRk91MYY"
    assert b._youtube_uploads_playlist("https://www.youtube.com/channel/UCabc") == "UUabc"
