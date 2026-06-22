"""Minimal stdio MCP server used by :mod:`tests.unit.tools.test_mcp_stdio_e2e`.

Exposes a single ``echo`` tool that returns its ``text`` argument.
Kept deterministic and dependency-free so the e2e test doesn't need
external services, credentials, or network access — just a Python
interpreter that can ``pip install mcp``.

Usage:

    python tests/tools/fixtures/echo_stdio_mcp_server.py

``FastMCP.run()`` defaults to stdio transport, so the process
reads/writes MCP protocol frames on stdin/stdout.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test")


@mcp.tool()
def echo(text: str) -> str:
    """
    Return *text* verbatim, prefixed with ``"echo: "``.

    Prefix is present so the test assertion can distinguish the
    tool's output from any echo of the request that might come
    from MCP machinery or logging — a bare passthrough would
    match too loosely.

    :param text: The string to echo back, e.g. ``"hello"``.
    :returns: ``f"echo: {text}"``.
    """
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()
