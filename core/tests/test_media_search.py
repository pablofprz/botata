"""Catálogo y fuentes declaradas compiten de igual a igual (2026-07-28).

Caso real que lo motivó: a "@botata una foto de un mapache please" el bot
contestó con un link markdown y una URL de CDN inventada, sin adjuntar nada —
teniendo un tablero de Pinterest declarado como `mapaches`. Tres fallas
encadenadas:

1. La búsqueda híbrida SIEMPRE devuelve el vecino más cercano. Sin umbral de
   relevancia, "mapache" resolvía a un carpincho (dist 0.503 medida contra el
   catálogo real) y, como "encontró algo", el fallback a las fuentes en vivo
   —que ya existía— no corría nunca.
2. `GenerateReplyNode` descartaba `outcome.image_path`: la tool encontraba la
   imagen y el reply la tiraba.
3. El prompt le ordenaba incluir "ESE link" de los resultados de tool; sin link
   que copiar, lo inventó.

El umbral (0.47) sale de medir el catálogo real: los que el catálogo TIENE dan
0.31–0.43 y los que no, 0.50–0.56.
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

_CERCA = 0.31      # el catálogo lo tiene de verdad
_LEJOS = 0.55      # lo más cercano, pero no es lo que se pidió


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "media.db")


@pytest.fixture()
def escenario(monkeypatch, tmp_path):
    """Catálogo con memes + un tablero de Pinterest declarado como `mapaches`."""
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "membrilla", "category": "memes", "name": "memes",
         "sources": ["cuenta"], "enabled": True},
        {"type": "pinterest", "category": "mapaches", "name": "mapaches",
         "description": "fotos de mapaches", "sources": ["polci/mapaches"],
         "enabled": True},
    ])
    monkeypatch.setattr(b.dbmod, "prefer_fresh_media", lambda rows: rows)
    monkeypatch.setattr(b, "_postable_media_path", lambda p: "/abs/" + p)
    monkeypatch.setattr(b.dbmod, "mark_image_used", lambda *a: None)
    monkeypatch.setattr(b, "_descargar_media", lambda url: str(tmp_path / "pin.jpg"))
    monkeypatch.setattr(b, "_pinterest_board_items", lambda ref, limit=15: [
        {"image_url": "https://i.pinimg.com/a.jpg", "url": "https://pin/1",
         "title": "un mapache lavando algo", "source": ref}])


def _catalogo(monkeypatch, dist, *, desc="un carpincho con la camiseta"):
    monkeypatch.setattr(b.dbmod, "hybrid_search_image_catalog",
                        lambda *a, **k: [{"id": 1, "file_path": "scrape/x.jpg",
                                          "description": desc, "category": "meme",
                                          "vec_distance": dist}])


# ─── El umbral de relevancia ────────────────────────────────────────────────
def test_el_catalogo_lejano_se_descarta_y_gana_la_fuente(conn, escenario, monkeypatch):
    """EL CASO DEL MAPACHE: el catálogo devuelve un carpincho, se descarta, y
    la foto sale del tablero que el admin declaró."""
    _catalogo(monkeypatch, _LEJOS)
    out = b._tool_search_images({"query": "foto de un mapache"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path and out.image_path.endswith("pin.jpg")
    assert "mapache" in out.text


def test_el_catalogo_cercano_se_usa(conn, escenario, monkeypatch):
    _catalogo(monkeypatch, _CERCA, desc="un meme de gato")
    out = b._tool_search_images({"query": "meme de gato"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path == "/abs/scrape/x.jpg"


def test_la_descripcion_literal_vale_aunque_el_vector_este_lejos(conn, escenario, monkeypatch):
    """Si la descripción DICE la palabra pedida, es un match real por más que el
    embedding no lo refleje."""
    _catalogo(monkeypatch, _LEJOS, desc="un carpincho tomando mate")
    out = b._tool_search_images({"query": "carpincho"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path == "/abs/scrape/x.jpg"


def test_una_palabra_del_envoltorio_no_alcanza_para_ser_relevante(conn, escenario, monkeypatch):
    """Encontrado verificando contra el catálogo real: a "una foto de un mapache
    please" contestaba con un chihuahua porque su descripción decía "foto".
    El envoltorio del pedido no cuenta como coincidencia."""
    _catalogo(monkeypatch, _LEJOS, desc="una foto de un chihuahua con colcha")
    out = b._tool_search_images({"query": "una foto de un mapache please"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path.endswith("pin.jpg")       # se fue a Pinterest, no al perro


def test_un_candidato_solo_de_fts_se_mira_por_descripcion(conn, escenario, monkeypatch):
    """`vec_distance` en None = entró solo por FTS (fuera de los vecinos más
    cercanos). No se acepta a ciegas."""
    _catalogo(monkeypatch, None, desc="una foto de un chihuahua")
    assert b._tool_search_images({"query": "una foto de un mapache"},
                                 ToolContext(state={}, conn=conn)).image_path.endswith("pin.jpg")
    _catalogo(monkeypatch, None, desc="un mapache en una jaula")
    assert b._tool_search_images({"query": "un mapache", "prefer": "catalogo"},
                                 ToolContext(state={}, conn=conn)).image_path == "/abs/scrape/x.jpg"


def test_sin_señal_de_distancia_se_le_cree_al_catalogo(conn, escenario, monkeypatch):
    """Compatibilidad: un caller viejo que ni trae la clave no se rompe."""
    monkeypatch.setattr(b.dbmod, "hybrid_search_image_catalog",
                        lambda *a, **k: [{"id": 1, "file_path": "scrape/x.jpg",
                                          "description": "algo", "category": "meme"}])
    out = b._tool_search_images({"query": "cualquier cosa"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path == "/abs/scrape/x.jpg"


def test_la_relevancia_manda_sobre_la_frescura(conn, escenario, monkeypatch):
    """El orden importa: si la frescura se aplica ANTES del filtro de relevancia,
    el 'mejor' pasa a ser el más nuevo y no el que se parece a lo pedido."""
    monkeypatch.setattr(b.dbmod, "hybrid_search_image_catalog", lambda *a, **k: [
        {"id": 1, "file_path": "scrape/perro.jpg", "description": "un chihuahua",
         "category": "meme", "vec_distance": _LEJOS},
        {"id": 2, "file_path": "scrape/gato.jpg", "description": "un gato atigrado",
         "category": "foto", "vec_distance": _CERCA},
    ])
    # prefer_fresh_media pone primero al perro (el "más fresco"): igual gana el gato.
    monkeypatch.setattr(b.dbmod, "prefer_fresh_media",
                        lambda rows: sorted(rows, key=lambda r: r["id"]))
    out = b._tool_search_images({"query": "un gato tierno", "prefer": "catalogo"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path == "/abs/scrape/gato.jpg"


def test_si_no_sirve_ninguno_lo_dice_sin_inventar(conn, escenario, monkeypatch):
    _catalogo(monkeypatch, _LEJOS)
    out = b._tool_search_images({"query": "un submarino nuclear"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path is None and "no encontré" in out.text


# ─── Quién gana: `prefer` y `source_weight` ─────────────────────────────────
def test_prefer_fuentes_fuerza_la_fuente_aunque_el_catalogo_sirva(conn, escenario, monkeypatch):
    _catalogo(monkeypatch, _CERCA, desc="un mapache indexado")
    out = b._tool_search_images({"query": "mapaches", "prefer": "fuentes"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path.endswith("pin.jpg")


def test_prefer_catalogo_fuerza_el_catalogo(conn, escenario, monkeypatch):
    _catalogo(monkeypatch, _CERCA, desc="un mapache indexado")
    monkeypatch.setattr(b, "_traer_de_fuentes_en_vivo",
                        lambda *a, **k: pytest.fail("no debía ir en vivo"))
    out = b._tool_search_images({"query": "mapaches", "prefer": "catalogo"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path == "/abs/scrape/x.jpg"


def test_prefer_catalogo_cae_a_la_fuente_si_el_catalogo_no_sirve(conn, escenario, monkeypatch):
    """`prefer` es una preferencia, no una mordaza: si el preferido no tiene
    nada, se usa el otro antes que contestar con las manos vacías."""
    _catalogo(monkeypatch, _LEJOS)
    out = b._tool_search_images({"query": "mapaches", "prefer": "catalogo"},
                                ToolContext(state={}, conn=conn))
    assert out.image_path.endswith("pin.jpg")


def test_source_weight_fija_la_prioridad_sin_tocar_codigo(conn, escenario, monkeypatch):
    _catalogo(monkeypatch, _CERCA, desc="un mapache indexado")
    monkeypatch.setattr(b, "SOURCE_WEIGHT", 0.0)      # siempre catálogo
    assert b._tool_search_images({"query": "mapaches"},
                                 ToolContext(state={}, conn=conn)).image_path == "/abs/scrape/x.jpg"
    monkeypatch.setattr(b, "SOURCE_WEIGHT", 1.0)      # siempre fuentes
    assert b._tool_search_images({"query": "mapaches"},
                                 ToolContext(state={}, conn=conn)).image_path.endswith("pin.jpg")


def test_search_videos_no_mira_fuentes_en_vivo(conn, escenario, monkeypatch):
    """Los conectores en vivo traen imágenes; para video el catálogo es el único
    camino y un umbral alto tiene que devolver 'no encontré', no un pin."""
    _catalogo(monkeypatch, _LEJOS)
    monkeypatch.setattr(b, "_traer_de_fuentes_en_vivo",
                        lambda *a, **k: pytest.fail("no hay video en vivo"))
    out = b._tool_search_videos({"query": "mapaches"}, ToolContext(state={}, conn=conn))
    assert out.image_path is None and "no encontré" in out.text


# ─── El pedido en prosa contra la descripción del admin ─────────────────────
@pytest.mark.parametrize("pedido", [
    "mapaches", "mapache", "una foto de un mapache",
    "foto de mapaches please", "MAPACHES",
])
def test_la_fuente_se_encuentra_aunque_el_pedido_venga_en_prosa(pedido):
    entry = {"category": "mapaches", "name": "mapaches",
             "description": "fotos de mapaches", "sources": ["polci/mapaches"]}
    assert b._entry_matches_topic(entry, pedido)


@pytest.mark.parametrize("pedido", ["un gato tierno", "meme de futbol", "noticias"])
def test_la_fuente_no_matchea_cualquier_cosa(pedido):
    entry = {"category": "mapaches", "name": "mapaches",
             "description": "fotos de mapaches", "sources": ["polci/mapaches"]}
    assert not b._entry_matches_topic(entry, pedido)


def test_el_puntaje_ignora_el_envoltorio_del_pedido():
    entry = {"category": "mapaches", "sources": ["polci/mapaches"]}
    # "una foto de" es envoltorio: no diluye el puntaje.
    assert b.source_match_score(entry, "una foto de mapaches") == 1.0
    assert b.source_match_score(entry, "mapaches y gatos") == 0.5


def test_un_pedido_de_puro_envoltorio_no_matchea_nada():
    assert b.source_match_score({"category": "mapaches"}, "una foto por favor") == 0.0


# ─── `mode`: lo último vs. cualquiera ───────────────────────────────────────
@pytest.fixture()
def tablero(monkeypatch, tmp_path):
    monkeypatch.setattr(b, "BASE_DIR", tmp_path)
    monkeypatch.setattr(b, "SOURCES", [
        {"type": "pinterest", "category": "mapaches", "sources": ["polci/mapaches"],
         "enabled": True}])
    monkeypatch.setattr(b, "_descargar_media", lambda url: str(tmp_path / "x.jpg"))
    monkeypatch.setattr(b, "_pinterest_board_items", lambda ref, limit=15: [
        {"image_url": f"https://i/{n}.jpg", "url": f"https://pin/{n}",
         "title": f"pin {n}", "source": ref} for n in range(10)])


def test_mode_last_trae_siempre_el_primero_del_feed(conn, tablero):
    """El feed viene del más nuevo al más viejo: 'lo último' es el índice 0."""
    for _ in range(8):
        out = b._tool_get_latest_media({"topic": "mapaches", "mode": "last"},
                                       ToolContext(state={}, conn=conn))
        assert "pin 0" in out.text and out.text.startswith("último de")


def test_mode_random_es_el_default_y_varia(conn, tablero):
    vistos = {b._tool_get_latest_media({"topic": "mapaches"},
                                       ToolContext(state={}, conn=conn)).text
              for _ in range(40)}
    assert len(vistos) > 1                      # no está clavado en uno solo


def test_le_pide_a_la_fuente_todo_lo_que_da(conn, tablero, monkeypatch):
    """El RSS de un tablero sirve ~26 pins; con el limit viejo de 15 se tiraba
    un tercio del material y el bot repetía de más."""
    pedidos = []
    monkeypatch.setattr(b, "_pinterest_board_items",
                        lambda ref, limit=15: pedidos.append(limit) or [
                            {"image_url": "https://i/a.jpg", "url": "https://pin/a",
                             "title": "a", "source": ref}])
    b._tool_get_latest_media({"topic": "mapaches"}, ToolContext(state={}, conn=conn))
    assert pedidos == [b._LIVE_POOL] and b._LIVE_POOL >= 26


# ─── La causa raíz: el reply tiraba la imagen que la tool encontró ──────────
class _FakeCallFn:
    def __init__(self, name, args):
        self.name, self.arguments = name, __import__("json").dumps(args)


class _FakeCall:
    def __init__(self, name, args):
        self.function = _FakeCallFn(name, args)


class _FakeLLM:
    """Fase 1 pide la tool; fase 2 devuelve el reply estructurado."""
    def __init__(self, calls, texto="ahí va"):
        self.calls, self.texto = calls, texto
        self.system_visto = ""

    def call_with_tools(self, system, user, tools):
        return "", self.calls

    def complete(self, system, user, schema):
        self.system_visto = system
        return b.BotReply(text=self.texto, should_update_profile=False,
                          image_search_query=None)


def _nodo_con_tool(monkeypatch, conn, resultado):
    from tools import Scope, ToolRegistry
    reg = ToolRegistry()
    reg.register("search_images", "d", {"type": "object", "properties": {}},
                 lambda args, ctx: resultado, {Scope.REPLY})
    llm = _FakeLLM([_FakeCall("search_images", {"query": "un mapache"})])
    monkeypatch.setattr(b, "soul_text", lambda: "SOUL")
    monkeypatch.setattr(b, "load_text", lambda p: "PROMPT")
    return b.GenerateReplyNode(llm=llm, conn=conn, registry=reg), llm


_ESTADO = {"author_handle": "ppolci.com", "mention_text": "una foto de un mapache",
           "mention_uri": "at://m/1", "thread_context": "", "is_admin": True}


def test_la_imagen_que_trajo_la_tool_llega_al_post(conn, monkeypatch):
    """EL BUG DE FONDO (2026-07-28): la tool devolvía `image_path` y el nodo solo
    leía `outcome.text`, así que el adjunto se perdía y el LLM terminaba
    escribiendo un link inventado para simularlo."""
    nodo, _ = _nodo_con_tool(monkeypatch, conn,
                             b.ToolResult(text="de polci/mapaches: un mapache",
                                          image_path="/abs/mapache.jpg"))
    assert nodo.run(dict(_ESTADO))["image_path"] == "/abs/mapache.jpg"


def test_al_LLM_se_le_avisa_que_la_imagen_ya_va_adjunta(conn, monkeypatch):
    """Cerrar el otro flanco: si no sabe que ya está adjunta, la narra o la
    linkea. El aviso va en el contexto de la fase 2."""
    nodo, llm = _nodo_con_tool(monkeypatch, conn,
                               b.ToolResult(text="un mapache", image_path="/abs/m.jpg"))
    nodo.run(dict(_ESTADO))
    assert "YA queda adjunta" in llm.system_visto


def test_sin_imagen_de_tool_sigue_valiendo_la_busqueda_del_catalogo(conn, monkeypatch):
    nodo, _ = _nodo_con_tool(monkeypatch, conn, b.ToolResult(text="no encontré nada"))
    monkeypatch.setattr(b, "resolve_catalog_image", lambda c, q, **k: "/abs/fallback.jpg")
    monkeypatch.setattr(nodo, "_resolve_image", lambda q: "/abs/fallback.jpg")
    assert nodo.run(dict(_ESTADO))["image_path"] == "/abs/fallback.jpg"
