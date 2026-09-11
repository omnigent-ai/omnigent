"""E2E regression: a steady-state MCP auth failure must not permanently
wedge the connection.

Reproduces two linked failure modes:

* ``omnigent.tools.mcp`` / ``_run_lifecycle`` logs ``MCP server
  'enterprise-context' lifecycle task failed during steady state`` when
  the streamable-HTTP transport raises ``httpx.HTTPStatusError: 401
  Unauthorized`` mid-session (an upstream gateway bearer token expiring
  while the connection is live).
* Every subsequent tool dispatch then raises ``RuntimeError: MCP server
  'enterprise-context' has no live session -- call connect() before
  call_tool()`` and never reconnects.

The chain is real end to end: a streamable-HTTP MCP subprocess
(``tests/tools/fixtures/expiring_auth_http_mcp_server.py``) serves an
``ask`` tool normally until it is *armed*, after which every request
returns ``401 Unauthorized``. The test drives a real
:class:`omnigent.tools.mcp.McpServerConnection` to a live session, arms
the server so a mid-session tool call crashes the lifecycle task and
clears the session, then *recovers* the server (token refreshed) and
asserts the next tool call self-heals and succeeds.

Expected today (bug): the lifecycle death clears ``_session``; the
``call_tool`` entry guard then short-circuits with ``has no live
session`` before the reconnect path can run, so the connection stays
wedged even after the transient auth failure clears -- the recovered
call raises instead of reconnecting, and the assertion fails. After the
fix a null session at call entry must be reconnected, so the recovered
call succeeds and the test passes.
"""

from __future__ import annotations

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

_EXPIRING_AUTH_SERVER = str(
    Path(__file__).parent / "fixtures" / "expiring_auth_http_mcp_server.py"
)

# Probe token the ask tool must round-trip on the recovered call.
# Obviously synthetic so nothing else in the chain can produce it.
_PROBE = "steady-state-recovery-probe"

# Per-call MCP read timeout (seconds). Bounds any pending request left
# doomed by the mid-session transport crash.
_MCP_TIMEOUT_S = 8

# Hard cap on each driven call so a regression can't hang the suite.
_CALL_DEADLINE_S = 60


def _free_port() -> int:
    """Reserve an ephemeral localhost port and return it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_listen(port: int, timeout_s: float = 30.0) -> None:
    """Poll until ``127.0.0.1:port`` accepts a TCP connection."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout_s}s")


@pytest.fixture()
def _no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep loopback traffic direct, off any corporate HTTP(S) proxy.

    CI sandboxes export ``HTTP_PROXY``/``HTTPS_PROXY``; httpx honors
    them even for 127.0.0.1, which would route the MCP traffic (and the
    control-endpoint calls) through the proxy and distort the failure
    mode.
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
def expiring_auth_http_mcp(
    _no_env_proxy: None,
) -> Iterator[tuple[MCPServerConfig, str]]:
    """A real HTTP MCP server whose auth expires on demand.

    Yields ``(config, base_url)``: ``config`` is an
    :class:`MCPServerConfig` pointing at the server's ``/mcp`` endpoint,
    and ``base_url`` is the origin the test hits ``/arm`` and ``/reset``
    on to toggle the 401 outage.
    """
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, _EXPIRING_AUTH_SERVER, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_listen(port)
        base_url = f"http://127.0.0.1:{port}"
        config = MCPServerConfig(
            name="enterprise-context",
            transport="http",
            url=f"{base_url}/mcp",
            # A few quick reconnect retries; jitter off for determinism.
            retry=RetryPolicy(
                max_retries=2,
                backoff_base_s=0.2,
                backoff_max_s=0.5,
                jitter=False,
            ),
            timeout=_MCP_TIMEOUT_S,
        )
        yield (config, base_url)
    finally:
        server.kill()
        server.wait(timeout=10)


@pytest.mark.asyncio
async def test_mcp_reconnects_after_steady_state_auth_expiry(
    expiring_auth_http_mcp: tuple[MCPServerConfig, str],
) -> None:
    """A transient steady-state 401 must not permanently wedge the MCP
    connection: once auth recovers, the next tool call must reconnect.

    Drives the expired-bearer journey end to end at the transport layer:

    1. Connect and discover the ``ask`` tool; a warm call proves the
       happy path.
    2. Arm the server so its bearer token has "expired": a mid-session
       tool call now returns ``401 Unauthorized``. This crashes the
       streamable-HTTP lifecycle task and clears the live session.
    3. Recover the server (token refreshed / gateway healthy again).
    4. A fresh ``ask`` call must self-heal by reconnecting and succeed.

    On an unfixed tree step 2 leaves ``_session`` cleared, and the
    ``call_tool`` null-session guard raises ``has no live session``
    before the reconnect path can run -- so step 4 raises instead of
    returning, and this test fails.
    """
    config, base_url = expiring_auth_http_mcp
    conn = McpServerConnection(config)
    try:
        tools = await conn.connect()
        assert any(t.name == "ask" for t in tools), (
            f"ask tool not discovered; got {[t.name for t in tools]}"
        )

        # Warm call proves the happy path works before auth expires.
        warm = await conn.call_tool("ask", {"question": "warm"})
        assert warm == "answer: warm", f"unexpected warm result: {warm!r}"

        # Auth "expires": every /mcp request now returns 401.
        async with httpx.AsyncClient() as client:
            armed = await client.get(f"{base_url}/arm", timeout=10)
            assert armed.status_code == 200, f"arm failed: {armed.status_code}"

        # A tool call during the outage crashes the lifecycle task and
        # clears the live session. Its own failure shape is not what
        # this test guards, so accept any error here.
        with pytest.raises(Exception):  # noqa: B017 - 401 surfaces varied types
            await conn.call_tool("ask", {"question": "during-outage"})

        # Precondition for the wedge: the steady-state crash left the
        # connection with no live session.
        assert conn._session is None, (
            "expected the mid-session 401 to clear the live session "
            "(the wedged state); connection still has a session"
        )

        # Auth recovers: token refreshed, gateway healthy again.
        async with httpx.AsyncClient() as client:
            reset = await client.get(f"{base_url}/reset", timeout=10)
            assert reset.status_code == 200, f"reset failed: {reset.status_code}"

        # The next tool call must reconnect and succeed. On the buggy
        # tree this raises RuntimeError("... has no live session -- call
        # connect() before call_tool()") because the null-session guard
        # short-circuits ahead of the reconnect path.
        recovered = await conn.call_tool("ask", {"question": _PROBE})
        assert recovered == f"answer: {_PROBE}", (
            "MCP connection did not self-heal after a transient "
            "steady-state auth failure cleared; the call after recovery "
            f"should reconnect and return the answer. got: {recovered!r}"
        )
    finally:
        await conn.close()
