"""E2E regression guard: a steady-state 401 must not permanently wedge
an HTTP MCP connection.

Reproduces the demo-memory failure mode where a remote streamable-HTTP
MCP server starts returning ``401 Unauthorized`` mid-session (an
expired/rotated bearer) and the Omnigent MCP client never recovers even
after auth is restored:

* ``omnigent.tools.mcp / _run_lifecycle`` logs
  ``MCP server '<name>' lifecycle task failed during steady state`` with a
  stack tail ending in
  ``httpx.HTTPStatusError: Client error '401 Unauthorized'`` — the 401
  crashes the streamable-HTTP transport task group, the lifecycle task
  exits, and its ``finally`` nulls ``_session`` (facet A: lifecycle death).
* Every subsequent dispatch then hits the ``if self._session is None``
  guard at the top of ``call_tool`` and raises
  ``RuntimeError: MCP server '<name>' has no live session — call connect()
  before call_tool()`` — surfaced by the runner as
  ``MCP tool dispatch failed for <name>__<tool>`` (facet B: permanent
  dispatch wedge). This fires on *every* later call, even after the remote
  is healthy again, because the guard short-circuits before the
  reconnect-retry path can run.

The chain is real end to end: a FastMCP streamable-HTTP subprocess serves
an ``echo`` tool behind an ASGI shim that returns ``401`` while a toggle
file exists, so the test can flip auth off and back on at runtime exactly
as an expiring/refreshing bearer would. A real
:class:`omnigent.tools.mcp.McpServerConnection` (the same client the runner
pools) is driven through three turns:

1. healthy call → succeeds (happy path, pre-fault);
2. call while the server returns 401 → fails (auth genuinely down — this
   must fail before and after any fix, and it tears the lifecycle down);
3. call *after* auth is restored → **must recover and succeed**.

Expected on the unfixed tree: turn 3 raises the ``has no live session``
RuntimeError instantly instead of reconnecting, so the assertion fails.
After the fix a recovered 401 must let the connection reconnect and the
call round-trip, so the same test passes.

This drives the client boundary rather than the web SPA because the runner
wraps ``call_tool`` in a workflow-level breaker (5 outer retries × 3
reconnects) that wedges the whole agent turn for minutes on the 401 and can
evict/mask the pooled connection, so the UI harness cannot reliably reach
the wedged state; the deterministic, fixable defect lives entirely in the
MCP client lifecycle/dispatch, which this reproduces faithfully over a real
HTTP transport.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.spec.types import MCPServerConfig, RetryPolicy
from omnigent.tools.mcp import McpServerConnection

# Probe tokens the echo tool round-trips. Obviously synthetic so nothing
# else in the chain can produce them by accident.
_BEFORE = "before-401-probe"
_DURING = "during-401-probe"
_AFTER = "after-recovery-probe"

# Per-call MCP session timeout. Bounds any single request.
_MCP_TIMEOUT_S = 10

# Hard cap on each awaited call so a regression can't hang the suite.
_CALL_DEADLINE_S = 60

# FastMCP echo server wrapped in an ASGI shim that returns 401 for every
# HTTP request while the toggle file exists, and forwards to the real MCP
# app otherwise. A file toggle is used because it is the simplest signal
# the test process can flip in the already-running server subprocess.
_SERVER_CODE = """
import os
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.types import Receive, Scope, Send

port = int(sys.argv[1])
deny_flag = sys.argv[2]

mcp = FastMCP("echo-http-401", host="127.0.0.1", port=port)


@mcp.tool()
def echo(text: str) -> str:
    return f"echo: {text}"


inner = mcp.streamable_http_app()


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "http" and os.path.exists(deny_flag):
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"unauthorized"})
        return
    await inner(scope, receive, send)


uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
"""


def _free_port() -> int:
    """Reserve an ephemeral localhost port and return it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(port: int, timeout_s: float = 30.0) -> None:
    """Poll the MCP endpoint until it completes an initialize handshake.

    Uses ``trust_env=False`` so the readiness probe never routes loopback
    through a corporate HTTP proxy (CI sandboxes export one).
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp-401-probe", "version": "0"},
        },
    }
    deadline = time.monotonic() + timeout_s
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.post(
                    f"http://127.0.0.1:{port}/mcp",
                    json=body,
                    headers={"Accept": "application/json, text/event-stream"},
                    timeout=2,
                )
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    raise TimeoutError(f"MCP server on 127.0.0.1:{port} not ready after {timeout_s}s")


@pytest.fixture()
def _no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep loopback traffic direct, off any corporate HTTP(S) proxy.

    CI sandboxes export ``HTTP_PROXY``/``HTTPS_PROXY``; httpx honors them
    even for 127.0.0.1, which would route the MCP traffic (and the injected
    401) through the proxy and distort the failure mode.
    """
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture()
def toggle_401_http_mcp(
    _no_env_proxy: None, tmp_path: Path
) -> Iterator[tuple[MCPServerConfig, Path]]:
    """A real HTTP MCP echo server whose auth can be toggled at runtime.

    Yields ``(config, deny_flag)``: the :class:`MCPServerConfig` points at
    the server, and the test switches the server to returning ``401`` for
    every request by creating ``deny_flag`` (and back to healthy by deleting
    it).
    """
    port = _free_port()
    deny_flag = tmp_path / "deny_401.flag"
    server = subprocess.Popen(
        [sys.executable, "-c", _SERVER_CODE, str(port), str(deny_flag)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_ready(port)
        config = MCPServerConfig(
            name="demo-memory",
            transport="http",
            url=f"http://127.0.0.1:{port}/mcp",
            # Bound the failing turn: a few reconnect attempts with short,
            # jitter-free backoff so the 401 turn resolves in seconds
            # instead of the runner's minutes-long breaker budget.
            retry=RetryPolicy(
                max_retries=2,
                backoff_base_s=0.3,
                backoff_max_s=1.0,
                jitter=False,
            ),
            timeout=_MCP_TIMEOUT_S,
        )
        yield (config, deny_flag)
    finally:
        server.kill()
        server.wait(timeout=10)


@pytest.mark.asyncio
async def test_mcp_connection_recovers_after_steady_state_401(
    toggle_401_http_mcp: tuple[MCPServerConfig, Path],
) -> None:
    """A 401 that clears must not permanently wedge the MCP connection.

    Drives the reported journey against a real HTTP MCP server: a healthy
    call, then a call while the server returns 401 (auth down — the
    lifecycle tears down), then a call after auth is restored. A correct
    client reconnects on the third call and the tool round-trips; the
    unfixed tree short-circuits on the nulled session and raises
    ``... has no live session — call connect() before call_tool()`` forever.
    """
    config, deny_flag = toggle_401_http_mcp
    conn = McpServerConnection(config)
    try:
        tools = await conn.connect()
        assert any(t.name == "echo" for t in tools), (
            f"echo tool not discovered; got {[t.name for t in tools]}"
        )

        # Turn 1 — healthy: proves the happy path works before the fault.
        warm = await asyncio.wait_for(
            conn.call_tool("echo", {"text": _BEFORE}), timeout=_CALL_DEADLINE_S
        )
        assert warm == f"echo: {_BEFORE}", f"pre-fault call failed: {warm!r}"

        # Auth goes down: the server now returns 401 for every request,
        # exactly as an expired/rotated bearer would in production.
        deny_flag.write_text("deny")

        # Turn 2 — during 401: the call must fail because auth is genuinely
        # down (this stays true before and after any fix). On the current
        # tree this 401 also crashes the streamable-HTTP transport task
        # group, killing the lifecycle task and nulling the session
        # (facet A: "lifecycle task failed during steady state").
        with pytest.raises(Exception):  # noqa: B017 - 401 surfaces varied types
            await asyncio.wait_for(
                conn.call_tool("echo", {"text": _DURING}), timeout=_CALL_DEADLINE_S
            )

        # Auth is restored: fresh requests to the server would now succeed.
        deny_flag.unlink()

        # Turn 3 — after recovery: THE regression assertion. A healthy
        # client reconnects and the call round-trips. The defect
        # short-circuits on the nulled session and raises the "has no live
        # session" RuntimeError instantly (facet B: permanent dispatch
        # wedge), never attempting the reconnect the restored auth allows.
        try:
            result = await asyncio.wait_for(
                conn.call_tool("echo", {"text": _AFTER}), timeout=_CALL_DEADLINE_S
            )
        except Exception as exc:
            pytest.fail(
                "MCP connection did not recover after the 401 cleared; "
                "call_tool raised instead of reconnecting: "
                f"{type(exc).__name__}: {exc}"
            )
        assert result == f"echo: {_AFTER}", (
            f"MCP call did not recover after a transient 401 cleared; got: {result!r}"
        )
    finally:
        await conn.close()
