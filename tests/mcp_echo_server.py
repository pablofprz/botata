"""MCP server de prueba (stdio) — usado por tests/test_mcp_tools.py.

También sirve de PLANTILLA mínima para los futuros servers de scrapers
(T18 reddit, T19 x, T20 ig): FastMCP + tools tipadas + `mcp.run()` stdio.
Config en settings.json para probarlo a mano:

    "MCP": {
      "echo": { "transport": "stdio", "command": "python",
                "args": ["tests/mcp_echo_server.py"] }
    }
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Devuelve el texto recibido, prefijado."""
    return f"echo: {text}"


@mcp.tool()
def fail() -> str:
    """Siempre falla (para testear el camino isError)."""
    raise RuntimeError("fallo intencional")


if __name__ == "__main__":
    mcp.run()  # stdio por default
