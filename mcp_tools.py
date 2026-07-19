"""mcp_tools.py — cliente MCP → ToolRegistry (T29).

Conecta MCP servers externos (config: settings.json → sección MCP) y registra
sus tools en el ToolRegistry (T1) con un handler proxy. Infra genérica estilo
tools.py: no conoce botata; solo el contrato ToolRegistry/ToolResult.

Config por server:
    "MCP": {
      "reddit": {
        "transport": "stdio",             // "stdio" | "http" (streamable-http)
        "command": "python", "args": ["servers/reddit.py"], "env": {}, "cwd": null,
        "url": null,                       // requerido si transport = http
        "enabled": true,
        "scopes": ["admin"],              // DEFAULT admin: promover a reply/feed es opt-in
        "tool_filter": {"include": [], "exclude": []},   // globs sobre nombres MCP
        "call_timeout_s": 30,
        "connect_timeout_s": 20
      }
    }

Reglas duras:
- Tools MCP nacen scope `admin` (el bot es público; scope reply = superficie de
  prompt injection). La sección TOOLS de settings.json puede overridear después.
- El handler proxy NUNCA lanza: todo error (transporte, timeout, isError del
  server) vuelve como ToolResult de texto — el call site admin ejecuta sin try.
- Server que no conecta al arranque → warning y se omite; el bot arranca igual.

El SDK mcp es async; acá vive un event loop en thread daemon (`MCPBridge`).
Cada server corre en una task dedicada que entra y sale de sus propios context
managers (los cancel scopes de anyio no pueden cruzarse de task).
"""
from __future__ import annotations

import asyncio
import atexit
import fnmatch
import logging
import threading
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from tools import ALL_SCOPES, ToolContext, ToolRegistry, ToolResult

log = logging.getLogger("botata.mcp")

_DEFAULT_SCOPES: frozenset[str] = frozenset({"admin"})
_DEFAULT_CALL_TIMEOUT_S = 30.0
_DEFAULT_CONNECT_TIMEOUT_S = 20.0


# ─── Bridge async→sync ───────────────────────────────────────────────────────
class MCPBridge:
    """Event loop en thread de fondo que sostiene las sesiones MCP abiertas.

    API sync: `start_server` (conecta + lista tools), `call` (tools/call) y
    `shutdown` (cierra todo; registrado en atexit).
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-bridge", daemon=True)
        self._thread.start()
        self._sessions: dict[str, ClientSession] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        atexit.register(self.shutdown)

    # -- API sync ------------------------------------------------------------
    def start_server(self, name: str, cfg: dict) -> list[Any]:
        """Conecta al server `name` y devuelve sus tools MCP. Lanza si falla."""
        connect_timeout = float(cfg.get("connect_timeout_s", _DEFAULT_CONNECT_TIMEOUT_S))
        fut = asyncio.run_coroutine_threadsafe(self._spawn(name, cfg, connect_timeout), self._loop)
        return fut.result(connect_timeout + 5)

    def call(self, server: str, tool: str, args: dict | None, timeout: float) -> Any:
        """tools/call sobre la sesión viva de `server`. Lanza si falla."""
        session = self._sessions.get(server)
        if session is None:
            raise RuntimeError(f"MCP server '{server}' no conectado")
        fut = asyncio.run_coroutine_threadsafe(session.call_tool(tool, args or {}), self._loop)
        return fut.result(timeout)

    def shutdown(self) -> None:
        """Cierra sesiones y frena el loop. Idempotente; nunca lanza (atexit)."""
        try:
            if self._loop.is_closed():
                return
            for name in list(self._stops):
                try:
                    asyncio.run_coroutine_threadsafe(self._stop_one(name), self._loop).result(10)
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            if not self._loop.is_running():
                self._loop.close()
        except Exception:  # atexit: jamás propagar
            pass

    # -- Internals (corren en el loop del thread) ------------------------------
    async def _spawn(self, name: str, cfg: dict, connect_timeout: float) -> list[Any]:
        existing = self._sessions.get(name)
        if existing is not None:  # ya conectado (ej. segundo registry del mismo proceso)
            return (await existing.list_tools()).tools
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        task = asyncio.get_running_loop().create_task(self._server_task(name, cfg, ready, stop))
        self._stops[name] = stop
        self._tasks[name] = task
        try:
            return await asyncio.wait_for(asyncio.shield(ready), timeout=connect_timeout)
        except (Exception, asyncio.CancelledError):
            stop.set()  # la task dueña desarma sus context managers
            self._stops.pop(name, None)
            self._tasks.pop(name, None)
            raise

    async def _server_task(self, name: str, cfg: dict, ready: asyncio.Future, stop: asyncio.Event) -> None:
        """Ciclo de vida completo de UN server, en una sola task (cancel scopes)."""
        try:
            async with AsyncExitStack() as stack:
                transport = cfg.get("transport", "stdio")
                if transport == "stdio":
                    env = {**get_default_environment(), **(cfg.get("env") or {})}
                    params = StdioServerParameters(
                        command=cfg["command"], args=list(cfg.get("args") or []),
                        env=env, cwd=cfg.get("cwd"),
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                elif transport == "http":
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(cfg["url"], headers=cfg.get("headers")))
                else:
                    raise ValueError(f"transport inválido: '{transport}' (usar stdio | http)")
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                tools = (await session.list_tools()).tools
                self._sessions[name] = session
                ready.set_result(tools)
                await stop.wait()
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                log.warning("MCP server '%s' murió en runtime: %s", name, e)
        finally:
            self._sessions.pop(name, None)

    async def _stop_one(self, name: str) -> None:
        stop = self._stops.pop(name, None)
        task = self._tasks.pop(name, None)
        if stop:
            stop.set()
        if task:
            try:
                await asyncio.wait_for(task, timeout=8)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()


_bridge: MCPBridge | None = None


def get_bridge() -> MCPBridge:
    """Singleton lazy: el loop/thread se crea recién si hay config MCP."""
    global _bridge
    if _bridge is None:
        _bridge = MCPBridge()
    return _bridge


# ─── Registro en el ToolRegistry ─────────────────────────────────────────────
def _passes_filter(name: str, flt: dict) -> bool:
    include = flt.get("include") or []
    exclude = flt.get("exclude") or []
    if include and not any(fnmatch.fnmatch(name, pat) for pat in include):
        return False
    return not any(fnmatch.fnmatch(name, pat) for pat in exclude)


def _result_text(res: Any) -> tuple[str, bool]:
    """CallToolResult → (texto concatenado, isError). v1 texto-only."""
    parts = [b.text for b in getattr(res, "content", []) if getattr(b, "type", None) == "text"]
    text = "\n".join(parts).strip() or "[el server no devolvió texto]"
    return text, bool(getattr(res, "isError", False))


def _make_handler(bridge: MCPBridge, server: str, tool: str, timeout: float):
    def handler(args: dict, ctx: ToolContext) -> ToolResult:
        try:
            res = bridge.call(server, tool, args, timeout)
        except Exception as e:  # NUNCA propagar: el call site admin no tiene try
            log.warning("MCP call %s/%s falló: %s: %s", server, tool, type(e).__name__, e)
            return ToolResult(text=f"[mcp:{server}] error llamando '{tool}': {e}")
        text, is_error = _result_text(res)
        if is_error:
            return ToolResult(text=f"[mcp:{server}] '{tool}' devolvió error: {text}")
        return ToolResult(text=text)
    return handler


def _server_scopes(server: str, cfg: dict) -> frozenset[str]:
    raw = set(cfg.get("scopes") or _DEFAULT_SCOPES)
    bad = raw - ALL_SCOPES
    if bad:
        log.warning("MCP server '%s': scope(s) inválido(s) %s — ignorados", server, bad)
    valid = frozenset(raw & ALL_SCOPES)
    return valid or _DEFAULT_SCOPES


def register_mcp_tools(registry: ToolRegistry, mcp_config: dict, *,
                       bridge: MCPBridge | None = None) -> int:
    """Conecta los servers habilitados y registra sus tools como `{server}_{tool}`.

    Devuelve cuántas tools registró. Un server caído se omite con warning
    (degradación graceful): el bot arranca igual.
    """
    bridge = bridge or get_bridge()
    count = 0
    for server, cfg in mcp_config.items():
        if not cfg.get("enabled", True):
            log.info("MCP server '%s' deshabilitado — omitido", server)
            continue
        try:
            tools = bridge.start_server(server, cfg)
        except Exception as e:
            log.warning("MCP server '%s' no disponible (%s: %s) — omitido",
                        server, type(e).__name__, e)
            continue
        scopes = _server_scopes(server, cfg)
        flt = cfg.get("tool_filter") or {}
        timeout = float(cfg.get("call_timeout_s", _DEFAULT_CALL_TIMEOUT_S))
        registered = 0
        for t in tools:
            if not _passes_filter(t.name, flt):
                continue
            reg_name = f"{server}_{t.name}"
            params = t.inputSchema or {"type": "object", "properties": {}}
            description = t.description or f"Tool '{t.name}' del server MCP '{server}'."
            try:
                registry.register(reg_name, description, params,
                                  _make_handler(bridge, server, t.name, timeout), scopes)
            except ValueError as e:  # colisión de nombre: no frenar el resto
                log.warning("MCP tool '%s' no registrada: %s", reg_name, e)
                continue
            registered += 1
        log.info("MCP server '%s': %d tool(s) registradas (scopes=%s)",
                 server, registered, sorted(scopes))
        count += registered
    return count
