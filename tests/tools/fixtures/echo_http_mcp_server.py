"""Minimal streamable-HTTP MCP server used by MCP transport e2e tests.

Exposes an ``echo`` tool that returns its ``text`` argument, plus a
``slow_echo`` variant that sleeps first so tests can inject a network
fault while a request is in flight (response pending).
Sibling of :mod:`tests.tools.fixtures.echo_stdio_mcp_server`, but
served over the streamable-HTTP transport so tests can interpose a
TCP proxy between client and server to simulate network faults.

Usage:

    python tests/tools/fixtures/echo_http_mcp_server.py <port>

Binds ``127.0.0.1:<port>`` and serves MCP at ``/mcp``.
"""

from __future__ import annotations

import asyncio
import sys

from mcp.server.fastmcp import FastMCP

port = int(sys.argv[1])
mcp = FastMCP("echo-http-test", host="127.0.0.1", port=port)


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


@mcp.tool()
async def slow_echo(text: str, delay_s: float) -> str:
    """
    Return ``f"echo: {text}"`` after sleeping *delay_s* seconds.

    Gives e2e tests a window in which the request has been accepted
    by the server but the response has not been sent, so a fault
    injected during the sleep lands exactly on the response path.

    :param text: The string to echo back, e.g. ``"hello"``.
    :param delay_s: Seconds to sleep before responding, e.g. ``2.0``.
    :returns: ``f"echo: {text}"``.
    """
    await asyncio.sleep(delay_s)
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
