"""Streamable-HTTP MCP server whose auth 'expires' mid-session on demand.

Exposes an ``ask`` tool (mirroring an enterprise-context Q&A tool).
The MCP handshake (``initialize`` / ``tools/list``) and
the first tool call succeed normally, so a client ``connect()`` reaches
steady state with a live session. Once the server is *armed* via
``GET /arm``, every subsequent request to the streamable-HTTP endpoint
returns ``401 Unauthorized`` -- the shape of an upstream gateway bearer
token expiring while an MCP connection sits in steady state.

This reproduces the transport-level trigger of the steady-state wedge:
a remote MCP server backed by an auth gateway returns ``401
Unauthorized`` mid-session; the streamable-HTTP client's lifecycle task
then crashes and clears the live session, and the next tool dispatch
raises ``has no live session``.

Usage::

    python tests/tools/fixtures/expiring_auth_http_mcp_server.py <port>

Binds ``127.0.0.1:<port>`` and serves MCP at ``/mcp``. Control routes:

* ``GET /arm``   -- start returning 401 on every ``/mcp`` request.
* ``GET /reset`` -- go back to serving normally.
"""

from __future__ import annotations

import sys

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, PlainTextResponse

port = int(sys.argv[1])
mcp = FastMCP("expiring-auth-http-test", host="127.0.0.1", port=port)


@mcp.tool()
def ask(question: str) -> str:
    """
    Return a canned answer for *question*, prefixed with ``"answer: "``.

    A simple request/response tool the agent calls during a turn.

    :param question: The question to answer, e.g. ``"hello"``.
    :returns: ``f"answer: {question}"``.
    """
    return f"answer: {question}"


# Toggled by the /arm and /reset control routes. Once armed, the ASGI
# guard below fails every streamable-HTTP request with 401, the way an
# expired gateway bearer token would.
_state = {"armed": False}

_mcp_app = mcp.streamable_http_app()


class _AuthExpiryGuard:
    """ASGI wrapper that 401s the MCP endpoint once armed.

    Non-HTTP scopes (notably ``lifespan``) pass straight through to the
    wrapped streamable-HTTP app so its session manager still starts and
    stops. The ``/arm`` and ``/reset`` control routes are handled here,
    ahead of the guard, so the test can flip auth state at will.
    """

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/arm":
            _state["armed"] = True
            await PlainTextResponse("armed")(scope, receive, send)
            return
        if path == "/reset":
            _state["armed"] = False
            await PlainTextResponse("reset")(scope, receive, send)
            return
        if _state["armed"]:
            await JSONResponse(
                {"error": "token expired"},
                status_code=401,
            )(scope, receive, send)
            return
        await self._inner(scope, receive, send)


app = _AuthExpiryGuard(_mcp_app)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
