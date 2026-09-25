"""Two-tool stdio MCP server for exercising a per-server ``tools:`` allow-list.

Exposes a *safe* tool (``echo``) and a *dangerous* tool (``danger``).
A spec that allow-lists only ``echo`` must never expose ``danger`` to
the model. This fixture stands in for a real-world server mixing many
read-only tools with one machine-global destructive tool (e.g.
``sandbox_clear``) whose allow-list exists precisely to exclude the
destructive tool.

Kept deterministic and dependency-free (only ``mcp``) so the e2e test
needs no external services, credentials, or network access.

Usage::

    python tests/tools/fixtures/allowlist_stdio_mcp_server.py

``FastMCP.run()`` defaults to stdio transport, so the process
reads/writes MCP protocol frames on stdin/stdout.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("allowlist-test")


@mcp.tool()
def echo(text: str) -> str:
    """
    Return *text* verbatim, prefixed with ``"echo: "`` (the SAFE tool).

    :param text: The string to echo back, e.g. ``"hello"``.
    :returns: ``f"echo: {text}"``.
    """
    return f"echo: {text}"


@mcp.tool()
def danger(text: str) -> str:
    """
    The tool an allow-list is meant to EXCLUDE.

    Returns a distinctive marker so a test can prove whether it ran.
    Stands in for a destructive tool like ``sandbox_clear``.

    :param text: An arbitrary string, echoed into the marker.
    :returns: ``f"danger-executed: {text}"``.
    """
    return f"danger-executed: {text}"


if __name__ == "__main__":
    mcp.run()
