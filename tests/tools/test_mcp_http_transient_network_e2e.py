"""E2E: transient network blips during MCP tool calls must be retried.

Guards the regression where the MCP tool-call retry path treats transient
network failures (connection reset mid-call, brief refusal window)
as permanent, so a short network blip fails the whole tool call —
and therefore the agent's turn — instead of reconnecting and
retrying like the LLM retry path does for ``ConnectionError`` /
``httpx.NetworkError``.

The chain is real end to end: a FastMCP streamable-HTTP subprocess
(``tests/tools/fixtures/echo_http_mcp_server.py``) is reached through
an in-process TCP proxy that can inject a controllable outage
("blip"): it hard-resets every live connection and refuses new ones
for a short window, then recovers. The test drives a real
:class:`omnigent.tools.mcp.McpServerConnection` through a warm call,
injects a blip shorter than the retry budget, and asserts the next
tool call still round-trips — which requires the connection layer to
classify the blip as transient and reconnect-retry.

Expected today (bug): the blipped call raises (``McpError`` request
timeout, or an ``httpx`` transport error) instead of retrying, so
the assertion fails. After the fix it must pass.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.spec.types import MCPServerConfig, RetryPolicy
from omnigent.tools.mcp import McpServerConnection

_ECHO_HTTP_SERVER = str(Path(__file__).parent / "fixtures" / "echo_http_mcp_server.py")

# Probe token the echo tool must round-trip. Obviously synthetic so
# nothing else in the chain can produce it by accident.
_PROBE = "transient-network-blip-probe"

# Outage length. Short enough that the retry budget below (0.5 +
# 1.0 + 2.0 = 3.5s of backoff across 3 retries) comfortably spans
# it; long enough that the first attempt reliably lands inside it.
_BLIP_SECONDS = 1.5

# Per-call MCP timeout. Bounds the failing (pre-fix) run: today the
# blipped call hangs on the dead session until this expires.
_MCP_TIMEOUT_S = 8

# Hard cap on the blipped call so a regression can't hang the suite.
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


_LINGER_RST = b"\x01\x00\x00\x00\x00\x00\x00\x00"  # SO_LINGER(on, 0) → RST on close


class _BlipProxy:
    """TCP forwarder with a controllable transient outage.

    Forwards ``127.0.0.1:listen_port`` → ``127.0.0.1:target_port``.
    :meth:`blip` hard-resets every live connection (RST) and refuses
    new ones for the given window, then recovers — the shape of a
    real transient network failure (VPN flap, LB restart).
    """

    def __init__(self, listen_port: int, target_port: int) -> None:
        self._target_port = target_port
        self._blip_until = 0.0
        self._active: list[socket.socket] = []
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", listen_port))
        self._srv.listen(16)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def blip(self, seconds: float) -> None:
        """Reset all live connections and refuse new ones for *seconds*."""
        self._blip_until = time.time() + seconds
        with self._lock:
            for s in self._active:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_RST)
                    s.close()
                except OSError:
                    pass
            self._active.clear()

    def close(self) -> None:
        """Stop accepting and drop every connection."""
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            for s in self._active:
                try:
                    s.close()
                except OSError:
                    pass
            self._active.clear()

    def _accept_loop(self) -> None:
        while True:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        if time.time() < self._blip_until:
            # Inside the outage window: refuse with a hard reset.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_RST)
            client.close()
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", self._target_port), timeout=5)
        except OSError:
            client.close()
            return
        with self._lock:
            self._active.extend([client, upstream])
        threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass


@pytest.fixture()
def _no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep loopback traffic direct, off any corporate HTTP(S) proxy.

    CI sandboxes export ``HTTP_PROXY``/``HTTPS_PROXY``; httpx honors
    them even for 127.0.0.1, which would route the MCP traffic (and
    the injected blip) through the proxy and distort the failure mode.
    """
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture()
def flaky_http_mcp(_no_env_proxy: None) -> Iterator[tuple[MCPServerConfig, _BlipProxy]]:
    """A real HTTP MCP echo server reachable only through a blip proxy.

    Yields ``(config, proxy)``: the :class:`MCPServerConfig` points
    at the proxy's listen port, and the test triggers the outage via
    ``proxy.blip(seconds)``.
    """
    server_port = _free_port()
    proxy_port = _free_port()
    server = subprocess.Popen(
        [sys.executable, _ECHO_HTTP_SERVER, str(server_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proxy: _BlipProxy | None = None
    try:
        _wait_for_listen(server_port)
        proxy = _BlipProxy(proxy_port, server_port)
        config = MCPServerConfig(
            name="flaky-http-echo",
            transport="http",
            url=f"http://127.0.0.1:{proxy_port}/mcp",
            # 3 retries with 0.5/1.0/2.0s backoff spans the 1.5s blip
            # with room to spare. jitter off for determinism.
            retry=RetryPolicy(
                max_retries=3,
                backoff_base_s=0.5,
                backoff_max_s=2.0,
                jitter=False,
            ),
            timeout=_MCP_TIMEOUT_S,
        )
        yield (config, proxy)
    finally:
        if proxy is not None:
            proxy.close()
        server.kill()
        server.wait(timeout=10)


@pytest.mark.asyncio
async def test_mcp_tool_call_survives_transient_network_blip(
    flaky_http_mcp: tuple[MCPServerConfig, _BlipProxy],
) -> None:
    """A network blip shorter than the retry budget must not fail the call.

    Drives the user-visible journey at the transport layer: an agent
    whose HTTP MCP server suffers a brief network outage during a
    tool call. With the configured retry policy (3 retries, 3.5s of
    backoff) and a 1.5s outage, a correctly-classified transient
    error reconnects and the call succeeds. An unfixed tree surfaces
    the blip as a non-retried exception (``McpError`` request timeout
    / ``httpx`` transport error), failing the turn.
    """
    config, proxy = flaky_http_mcp
    conn = McpServerConnection(config)
    try:
        tools = await conn.connect()
        assert any(t.name == "echo" for t in tools), (
            f"echo tool not discovered; got {[t.name for t in tools]}"
        )

        # Warm call proves the happy path works before the fault.
        warm = await conn.call_tool("echo", {"text": "warm"})
        assert warm == "echo: warm"

        # Inject the transient outage, then call while it is active.
        proxy.blip(_BLIP_SECONDS)
        result = await asyncio.wait_for(
            conn.call_tool("echo", {"text": _PROBE}),
            timeout=_CALL_DEADLINE_S,
        )
        assert result == f"echo: {_PROBE}", (
            f"tool call did not survive a {_BLIP_SECONDS}s network blip "
            f"despite a retry budget that spans it; got: {result!r}"
        )
    finally:
        await conn.close()
