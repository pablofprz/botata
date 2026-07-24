"""Tests de mcp_tools.py (T29): registro de tools MCP en el ToolRegistry.

Unit: bridge fake (sin SDK real). E2E: server echo real por stdio
(tests/mcp_echo_server.py) — levanta un subproceso python.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mcp_tools
from tools import Scope, ToolContext, ToolRegistry

_CTX = ToolContext(state={}, conn=None)


def _fake_tool(name="echo", description="una tool", schema=None):
    return SimpleNamespace(name=name, description=description,
                           inputSchema=schema or {"type": "object", "properties": {}})


def _text_result(text="hola", is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=is_error)


class FakeBridge:
    """Bridge sin MCP real: tools fijas + comportamiento inyectable en call."""

    def __init__(self, tools=None, call_result=None, call_exc=None, start_exc=None):
        self.tools = tools if tools is not None else [_fake_tool()]
        self.call_result = call_result or _text_result()
        self.call_exc = call_exc
        self.start_exc = start_exc
        self.start_calls = 0

    def start_server(self, name, cfg):
        self.start_calls += 1
        if self.start_exc:
            raise self.start_exc
        return self.tools

    def call(self, server, tool, args, timeout):
        if self.call_exc:
            raise self.call_exc
        return self.call_result


# ─── Unit ────────────────────────────────────────────────────────────────────
def test_prefixing_y_scope_admin_default():
    reg = ToolRegistry()
    n = mcp_tools.register_mcp_tools(reg, {"srv": {}}, bridge=FakeBridge())
    assert n == 1
    assert reg.get("srv_echo") is not None
    assert [t.name for t in reg.available(Scope.ADMIN)] == ["srv_echo"]
    assert reg.available(Scope.REPLY) == []          # admin-only por default
    assert reg.available(Scope.FEED_REFLECTION) == []


def test_scopes_explicitos_e_invalidos():
    reg = ToolRegistry()
    mcp_tools.register_mcp_tools(
        reg, {"srv": {"scopes": ["reply", "banana"]}}, bridge=FakeBridge())
    tool = reg.get("srv_echo")
    assert tool.scopes == frozenset({"reply"})       # inválido filtrado, válido queda


def test_tool_filter_include_exclude():
    tools = [_fake_tool("get_posts"), _fake_tool("get_users"), _fake_tool("delete_all")]
    reg = ToolRegistry()
    n = mcp_tools.register_mcp_tools(
        reg, {"srv": {"tool_filter": {"include": ["get_*"], "exclude": ["*users"]}}},
        bridge=FakeBridge(tools=tools))
    assert n == 1
    assert reg.names() == ["srv_get_posts"]


def test_server_deshabilitado_no_conecta():
    bridge = FakeBridge()
    reg = ToolRegistry()
    n = mcp_tools.register_mcp_tools(reg, {"srv": {"enabled": False}}, bridge=bridge)
    assert n == 0
    assert bridge.start_calls == 0


def test_server_caido_se_omite_sin_lanzar():
    bridge = FakeBridge(start_exc=ConnectionError("boom"))
    reg = ToolRegistry()
    n = mcp_tools.register_mcp_tools(reg, {"srv": {}}, bridge=bridge)
    assert n == 0
    assert reg.names() == []


def test_handler_atrapa_excepciones():
    """El call site admin ejecuta sin try/except: el handler jamás lanza."""
    reg = ToolRegistry()
    mcp_tools.register_mcp_tools(
        reg, {"srv": {}}, bridge=FakeBridge(call_exc=TimeoutError("timeout")))
    out = reg.execute("srv_echo", {}, _CTX)
    assert "[mcp:srv]" in out.text and "error" in out.text


def test_handler_is_error_del_server():
    reg = ToolRegistry()
    mcp_tools.register_mcp_tools(
        reg, {"srv": {}}, bridge=FakeBridge(call_result=_text_result("explotó", is_error=True)))
    out = reg.execute("srv_echo", {}, _CTX)
    assert "devolvió error" in out.text and "explotó" in out.text


def test_handler_texto_ok():
    reg = ToolRegistry()
    mcp_tools.register_mcp_tools(
        reg, {"srv": {}}, bridge=FakeBridge(call_result=_text_result("todo bien")))
    assert reg.execute("srv_echo", {}, _CTX).text == "todo bien"


def test_tools_config_overridea_tools_mcp():
    """apply_config (sección TOOLS) aplica también a nombres MCP."""
    reg = ToolRegistry()
    mcp_tools.register_mcp_tools(reg, {"srv": {}}, bridge=FakeBridge())
    reg.apply_config({"srv_echo": {"scopes": ["reply"], "enabled": True}})
    assert [t.name for t in reg.available(Scope.REPLY)] == ["srv_echo"]


def test_colision_de_nombre_no_frena():
    reg = ToolRegistry()
    reg.register("srv_echo", "ya existe", {"type": "object"}, lambda a, c: None, {Scope.ADMIN})
    n = mcp_tools.register_mcp_tools(
        reg, {"srv": {}}, bridge=FakeBridge(tools=[_fake_tool("echo"), _fake_tool("otra")]))
    assert n == 1                                    # echo colisionó, otra entró
    assert reg.get("srv_otra") is not None


# ─── E2E: server echo real por stdio ─────────────────────────────────────────
_ECHO_SERVER = str(Path(__file__).resolve().parent / "mcp_echo_server.py")
_ECHO_CFG = {"echo": {"transport": "stdio", "command": sys.executable,
                      "args": [_ECHO_SERVER], "call_timeout_s": 20}}


@pytest.fixture(scope="module")
def echo_registry():
    reg = ToolRegistry()
    bridge = mcp_tools.MCPBridge()
    n = mcp_tools.register_mcp_tools(reg, _ECHO_CFG, bridge=bridge)
    assert n == 2, "el server echo expone 2 tools (echo, fail)"
    yield reg
    bridge.shutdown()


def test_e2e_echo(echo_registry):
    out = echo_registry.execute("echo_echo", {"text": "hola mundo"}, _CTX)
    assert out.text == "echo: hola mundo"


def test_e2e_fail_es_graceful(echo_registry):
    out = echo_registry.execute("echo_fail", {}, _CTX)
    assert "[mcp:echo]" in out.text and "error" in out.text.lower()


def test_e2e_command_inexistente_se_omite():
    reg = ToolRegistry()
    bridge = mcp_tools.MCPBridge()
    try:
        cfg = {"roto": {"transport": "stdio", "command": "no-existe-este-binario-xyz",
                        "connect_timeout_s": 10}}
        assert mcp_tools.register_mcp_tools(reg, cfg, bridge=bridge) == 0
        assert reg.names() == []
    finally:
        bridge.shutdown()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
