"""Tests de HandleAdminCommandNode con múltiples tool calls (fix 2026-07-20):
las tools de contenido se ejecutan todas; las de config mantienen una por mensaje."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b
from tools import Scope, ToolRegistry, ToolResult

_STATE = {"author_handle": "ppolci.com", "mention_text": "agregá 3 temas", "is_admin": True}


class FakeCallFn:
    def __init__(self, name, args):
        self.name = name
        self.arguments = json.dumps(args)


class FakeCall:
    def __init__(self, name, args):
        self.function = FakeCallFn(name, args)


class FakeLLM:
    def __init__(self, calls):
        self.calls = calls

    def call_with_tools(self, system, user, tools):
        self.last_system = system
        return "", self.calls


def _registry(executed):
    reg = ToolRegistry()

    def content_handler(args, ctx):
        executed.append(("add_music", args["query"]))
        return ToolResult(text=f"agregué {args['query']}")

    def config_handler(args, ctx):
        executed.append(("set_task_config", args))
        return ToolResult(text="config cambiada")

    obj = {"type": "object", "properties": {}}
    reg.register("add_music_recommendation", "d", obj, content_handler, {Scope.ADMIN})
    # nombre real de una config tool para gatillar la guarda _CONFIG_TOOL_NAMES
    reg.register("set_task_config", "d", obj, config_handler, {Scope.ADMIN})
    return reg


def _run(calls, executed):
    node = b.HandleAdminCommandNode(FakeLLM(calls), conn=None, registry=_registry(executed))
    return node.run(dict(_STATE))


def test_ejecuta_todas_las_tools_de_contenido():
    executed = []
    out = _run([FakeCall("add_music_recommendation", {"query": q})
                for q in ("tema1", "tema2", "tema3")], executed)
    assert [q for _, q in executed] == ["tema1", "tema2", "tema3"]
    assert out["reply_text"] == "agregué tema1\nagregué tema2\nagregué tema3"


def test_config_solo_la_primera():
    executed = []
    out = _run([FakeCall("set_task_config", {"task": "news"}),
                FakeCall("set_task_config", {"task": "feed"})], executed)
    assert len([e for e in executed if e[0] == "set_task_config"]) == 1
    assert "salteada" in out["reply_text"]


def test_config_y_contenido_conviven():
    executed = []
    out = _run([FakeCall("set_task_config", {"task": "news"}),
                FakeCall("add_music_recommendation", {"query": "tema1"}),
                FakeCall("set_task_config", {"task": "feed"})], executed)
    # la de contenido corre aunque haya habido config antes; la 2da config no
    assert ("add_music", "tema1") in executed
    assert len([e for e in executed if e[0] == "set_task_config"]) == 1
    assert "config cambiada" in out["reply_text"] and "agregué tema1" in out["reply_text"]


def test_una_sola_tool_sigue_igual():
    executed = []
    out = _run([FakeCall("add_music_recommendation", {"query": "tema1"})], executed)
    assert out["reply_text"] == "agregué tema1"


def test_rutinas_actuales_entran_al_contexto(tmp_path, monkeypatch):
    """"Dejá de postear memes" tiene que resolverse a la rutina correcta por lo
    que HACE: la lista de rutinas (apagadas incluidas) entra al system prompt."""
    rd = tmp_path / "routines"
    rd.mkdir()
    (rd / "shitposting.md").write_text(
        "---\ninterval_hours: 4\n---\nposteá un meme, no repitas\n", encoding="utf-8")
    (rd / "apagada.md").write_text(
        "---\ninterval_hours: 2\nenabled: false\n---\ncompartí una canción\n", encoding="utf-8")
    monkeypatch.setattr(b, "ROUTINES_DIR", rd)
    llm = FakeLLM([FakeCall("add_music_recommendation", {"query": "x"})])
    node = b.HandleAdminCommandNode(llm, conn=None, registry=_registry([]))
    node.run(dict(_STATE))
    assert "RUTINAS ACTUALES" in llm.last_system
    assert "shitposting" in llm.last_system and "posteá un meme" in llm.last_system
    assert "apagada (OFF)" in llm.last_system  # las deshabilitadas también se ven
