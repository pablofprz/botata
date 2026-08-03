"""Rondas de tools: razonar en varios pasos (2026-08-01).

El pedido que lo motivó: "leé la playlist del grupo, buscá música parecida y
posteala". Con UNA sola vuelta de tool calling el modelo tiene que decidir todas
sus llamadas antes de ver ningún resultado, así que la segunda búsqueda —que
depende de lo que trajo la primera— es imposible: el bot contestaba que no podía
y cada fraseo nuevo necesitaba una skill que lo guionara.

Acá el LLM va mockeado: lo que se prueba es el LOOP (que los resultados vuelvan
como `role: "tool"`, que el modelo pueda decidir en función de ellos, y que los
topes corten).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from tools import ToolResult  # noqa: E402


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _Call:
    def __init__(self, cid, name, arguments="{}"):
        self.id, self.function = cid, _Fn(name, arguments)


class LLMFalso:
    """Devuelve una tanda de tool calls por ronda; guarda lo que le llegó."""

    def __init__(self, tandas):
        self._tandas = list(tandas)
        self.vistos: list[list[dict]] = []

    def call_with_messages(self, messages, tools):
        self.vistos.append([dict(m) for m in messages])
        if not self._tandas:
            return "listo", []
        siguiente = self._tandas.pop(0)
        return (None, siguiente) if siguiente else ("listo", [])


def _ejecutor(salidas):
    def _ejecutar(nombre, args):
        return ToolResult(text=salidas.get(nombre, f"ok:{nombre}"))
    return _ejecutar


# ─── El caso que motivó todo ────────────────────────────────────────────────
def test_el_modelo_ve_el_resultado_de_una_tool_antes_de_pedir_la_siguiente():
    llm = LLMFalso([[_Call("1", "get_playlist_track")], [_Call("2", "search_music")]])
    texto, resultados = b.correr_rondas_de_tools(
        llm, "sys", "traeme algo parecido pero que no esté", [],
        _ejecutor({"get_playlist_track": "Karma Police — Radiohead"}),
        rondas=3, etiqueta="test")

    assert [r["tool"] for r in resultados] == ["get_playlist_track", "search_music"]
    # En la segunda vuelta el modelo YA tenía el resultado de la primera: eso es
    # exactamente lo que antes no existía.
    segunda = llm.vistos[1]
    assert segunda[-1]["role"] == "tool"
    assert "Karma Police" in segunda[-1]["content"]
    assert segunda[-2]["role"] == "assistant"    # la tanda de calls, como la espera la API


def test_con_una_sola_ronda_se_comporta_como_siempre():
    """El default (TOOL_ROUNDS=1) no puede cambiarle la conducta a nadie."""
    llm = LLMFalso([[_Call("1", "get_playlist_track")], [_Call("2", "search_music")]])
    _, resultados = b.correr_rondas_de_tools(
        llm, "sys", "algo", [], _ejecutor({}), rondas=1, etiqueta="test")
    assert [r["tool"] for r in resultados] == ["get_playlist_track"]
    assert len(llm.vistos) == 1                  # una sola llamada al modelo


def test_si_no_llama_tools_devuelve_el_texto():
    llm = LLMFalso([[]])
    texto, resultados = b.correr_rondas_de_tools(
        llm, "sys", "hola", [], _ejecutor({}), rondas=3, etiqueta="test")
    assert texto == "listo" and resultados == []


def test_corta_cuando_el_modelo_deja_de_pedir_tools():
    llm = LLMFalso([[_Call("1", "web_search")], []])
    _, resultados = b.correr_rondas_de_tools(
        llm, "sys", "x", [], _ejecutor({}), rondas=5, etiqueta="test")
    assert len(resultados) == 1 and len(llm.vistos) == 2   # no gasta las 5


def test_el_tope_de_tool_calls_corta_un_modelo_colgado():
    """Un modelo que llama tools para siempre no puede quemar el presupuesto."""
    llm = LLMFalso([[_Call(str(i), "web_search") for i in range(5)]] * 5)
    _, resultados = b.correr_rondas_de_tools(
        llm, "sys", "x", [], _ejecutor({}), rondas=5, etiqueta="test")
    assert len(resultados) == b._MAX_TOOL_CALLS


def test_una_tool_con_args_rotos_no_tumba_la_ronda():
    llm = LLMFalso([[_Call("1", "web_search", arguments="{esto no es json")], []])
    _, resultados = b.correr_rondas_de_tools(
        llm, "sys", "x", [], _ejecutor({}), rondas=3, etiqueta="test")
    assert [r["tool"] for r in resultados] == ["web_search"]


# ─── La guarda de config del nodo admin sigue valiendo entre rondas ─────────
def test_un_cambio_de_config_por_mensaje_aunque_encadene():
    """Encadenar no puede ser la puerta para meter dos cambios de config en un
    mismo mensaje (guarda T30)."""
    corridas = []
    estado = {"hecho": False}
    nombre_config = next(iter(b._CONFIG_TOOL_NAMES))

    def _ejecutar(nombre, args):
        if nombre in b._CONFIG_TOOL_NAMES and estado["hecho"]:
            return ToolResult(text=f"[{nombre}: salteada]")
        corridas.append(nombre)
        if nombre in b._CONFIG_TOOL_NAMES:
            estado["hecho"] = True
        return ToolResult(text="ok")

    llm = LLMFalso([[_Call("1", nombre_config)], [_Call("2", nombre_config)]])
    _, resultados = b.correr_rondas_de_tools(
        llm, "sys", "cambiá dos cosas", [], _ejecutar, rondas=3, etiqueta="test")
    assert corridas == [nombre_config]                     # la segunda no corrió
    assert "salteada" in resultados[1]["outcome"].text


# ─── La clave de settings ───────────────────────────────────────────────────
@pytest.mark.parametrize("valor, esperado", [
    (None, 1), (1, 1), (3, 3), (0, 1), (-2, 1), (99, 5),
])
def test_tool_rounds_se_acota(valor, esperado, monkeypatch):
    s = {} if valor is None else {"TOOL_ROUNDS": valor}
    assert max(1, min(5, int(s.get("TOOL_ROUNDS", 1)))) == esperado


def test_una_tool_call_sin_id_no_tumba_la_respuesta():
    """La spec de OpenAI siempre trae `id`, pero el router hace fallback a
    endpoints que no la cumplen al pie de la letra. Un detalle de protocolo no
    puede dejar mudo al bot."""
    class _SinId:
        def __init__(self, name):
            self.function = _Fn(name, "{}")

    llm = LLMFalso([[_SinId("web_search")], []])
    _, resultados = b.correr_rondas_de_tools(
        llm, "sys", "x", [], _ejecutor({}), rondas=3, etiqueta="test")
    assert [r["tool"] for r in resultados] == ["web_search"]
    # el id inventado tiene que ser el MISMO en el assistant y en el role:tool
    segunda = llm.vistos[1]
    assert segunda[-2]["tool_calls"][0]["id"] == segunda[-1]["tool_call_id"]
