"""Local stdio MCP proxy to GitHub's hosted MCP that stamps an Omnigent link.

The harness connects to this proxy over stdio (a ``github`` MCP server); the
proxy forwards every request to GitHub's hosted MCP server
(:data:`omnigent.github_mcp.GITHUB_MCP_URL`), authenticated with the connected
user's token, and — on ``create_pull_request`` — appends an
``[Open in Omnigent](…/c/<session_id>)`` link to the PR ``body``. This is
deterministic (no reliance on the model), so the session-PR panel can associate
MCP-opened PRs by parsing that link, and the PR carries a click-through back to
the session.

Run as a stdio server: ``python -m omnigent.github_mcp_proxy``. It fetches the
GitHub token from the credential broker (never persisted) using the broker
coordinates the runner sets on the proxy subprocess (server URL + host id +
launch token), and reads the session URL from ``OMNIGENT_SESSION_URL``.
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack

import anyio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from omnigent.github_mcp import GITHUB_MCP_URL, github_mcp_token, inject_session_link

_logger = logging.getLogger(__name__)

#: The hosted-MCP tool whose PR body we stamp.
_PR_TOOL = "create_pull_request"


async def _list_all_upstream_tools(upstream: ClientSession) -> list[types.Tool]:
    """Fetch every tool from the upstream server, following pagination."""
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        page = await upstream.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools


async def _serve(session_url: str | None) -> None:
    token = github_mcp_token()
    if not token:
        raise SystemExit("github-mcp-proxy: no GitHub token from credential broker")

    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncExitStack() as stack:
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamablehttp_client(
                url=GITHUB_MCP_URL, headers=headers, timeout=30, sse_read_timeout=300
            )
        )
        upstream = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await upstream.initialize()

        server: Server = Server(name="omnigent-github")

        @server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            return await _list_all_upstream_tools(upstream)

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict) -> types.CallToolResult:
            if name == _PR_TOOL:
                arguments = inject_session_link(arguments, session_url)
            # Forward the upstream result verbatim (content, structuredContent,
            # isError) — the low-level Server accepts a CallToolResult return.
            return await upstream.call_tool(name, arguments)

        init_opts = server.create_initialization_options()
        async with stdio_server() as (stdin, stdout):
            await server.run(stdin, stdout, init_opts)


def main() -> None:
    session_url = (os.environ.get("OMNIGENT_SESSION_URL") or "").strip() or None
    anyio.run(_serve, session_url)


if __name__ == "__main__":
    main()
