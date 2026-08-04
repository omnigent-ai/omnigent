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
from collections.abc import Generator
from contextlib import AsyncExitStack

import anyio
import httpx
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from omnigent.github_mcp import GITHUB_MCP_URL, github_mcp_token, inject_session_link

_logger = logging.getLogger(__name__)

#: The hosted-MCP tool whose PR body we stamp.
_PR_TOOL = "create_pull_request"


class _BrokerAuth(httpx.Auth):
    """Bearer auth that refreshes the brokered GitHub token on a 401.

    A stdio MCP session can outlive the vended token's lifetime (~8h). The
    broker re-resolves the token server-side, so on a 401 we re-fetch it and
    retry the request once — instead of every tool call 401ing for the rest of
    the session. The token is cached between requests; we only hit the broker
    again after an actual 401.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401:
            fresh = github_mcp_token()
            if fresh and fresh != self._token:
                self._token = fresh
                request.headers["Authorization"] = f"Bearer {self._token}"
                yield request


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


async def _serve_empty() -> None:
    """Run a github MCP server that exposes no tools.

    Used when the session owner hasn't connected GitHub (the broker returns no
    token): the harness still gets a well-formed ``github`` server that
    initializes cleanly and simply lists nothing, rather than a subprocess that
    crashes or a dead server. Connecting GitHub and relaunching populates it.
    """
    server: Server = Server(name="omnigent-github")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return []

    init_opts = server.create_initialization_options()
    async with stdio_server() as (stdin, stdout):
        await server.run(stdin, stdout, init_opts)


async def _serve(session_url: str | None) -> None:
    token = github_mcp_token()
    if not token:
        # Owner not connected (or broker unreachable): degrade to an empty
        # server instead of crashing the harness's MCP startup.
        await _serve_empty()
        return

    # Connect to the upstream (token refreshes on 401 via _BrokerAuth). A
    # rejected token or unreachable GitHub during setup must not crash the
    # harness's MCP startup — degrade to an empty server, exactly like the
    # not-connected case.
    stack = AsyncExitStack()
    try:
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamablehttp_client(
                url=GITHUB_MCP_URL, auth=_BrokerAuth(token), timeout=30, sse_read_timeout=300
            )
        )
        upstream = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await upstream.initialize()
    except Exception:
        await stack.aclose()
        _logger.warning(
            "github proxy: upstream unavailable, serving no tools", exc_info=True
        )
        await _serve_empty()
        return

    try:
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
    finally:
        await stack.aclose()


def main() -> None:
    session_url = (os.environ.get("OMNIGENT_SESSION_URL") or "").strip() or None
    anyio.run(_serve, session_url)


if __name__ == "__main__":
    main()
