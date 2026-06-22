"""End-to-end TCP transport tests — real uvicorn, real localhost socket.

The Phase 3 transport mirrors Phase 2's UDS but on a TCP loopback
socket. The application protocol is identical; only the wire bytes
change.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.runner.transports.tcp import (
    RunnerTCPSubprocess,
    create_tcp_client,
)

# ``_entry:create_app`` requires RUNNER_SERVER_URL in the subprocess
# environment. Tests don't need a real Omnigent server — the env var only
# needs to satisfy the non-empty check so the factory can build the
# httpx client. The actual URL is never dialled during these transport
# smoke tests.
_FAKE_SERVER_ENV = {"RUNNER_SERVER_URL": "http://127.0.0.1:1"}

# ── Subprocess lifecycle ─────────────────────────────────


def test_runner_tcp_subprocess_starts_and_binds_port() -> None:
    """Context manager launches uvicorn on a TCP port and reports the port back."""
    with RunnerTCPSubprocess(extra_env=_FAKE_SERVER_ENV) as runner:
        # The OS-assigned port lives on .port and is reflected in .base_url.
        assert runner.port > 0
        assert runner.base_url == f"http://127.0.0.1:{runner.port}"


def test_runner_tcp_subprocess_with_explicit_port() -> None:
    """Caller-supplied port is honored.

    Using a high-numbered ephemeral port to avoid the rare collision
    with another process on the test host. We check the manager
    actually used what we passed.
    """
    # Probe for a free port via the OS, then pass it explicitly.
    from omnigent.runner.transports.tcp import _pick_free_port

    port = _pick_free_port()
    with RunnerTCPSubprocess(port=port, extra_env=_FAKE_SERVER_ENV) as runner:
        assert runner.port == port


def test_runner_tcp_subprocess_cleans_up_on_exit() -> None:
    with RunnerTCPSubprocess(extra_env=_FAKE_SERVER_ENV) as runner:
        process = runner._process
    assert process is not None
    assert process.poll() is not None, "TCP subprocess must be reaped on context-manager exit."


# ── Round-trip via the TCP client ────────────────────────


@pytest.mark.asyncio
async def test_health_round_trip_via_tcp() -> None:
    """The flagship Phase 3 test: server → TCP → uvicorn → runner FastAPI."""
    with RunnerTCPSubprocess(extra_env=_FAKE_SERVER_ENV) as runner:
        async with create_tcp_client(runner.base_url) as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_post_session_events_stub_via_tcp() -> None:
    """Session events endpoint is reachable via TCP.

    ``create_runner_app_from_env`` starts without a HarnessProcessManager
    (scaffold mode), so the endpoint returns 501. The test asserts that
    the route exists and the transport delivers the response — not that
    the runner is fully functional.
    """
    with RunnerTCPSubprocess(
        extra_env={"RUNNER_SERVER_URL": "http://127.0.0.1:1"},
    ) as runner:
        async with create_tcp_client(runner.base_url) as client:
            response = await client.post(
                "/v1/sessions/conv_test/events",
                json={"type": "message", "role": "user", "content": []},
            )
            assert response.status_code == 501


# ── Failure modes ────────────────────────────────────────


def test_tcp_subprocess_with_bad_app_factory_raises() -> None:
    """Bad import path → uvicorn crashes → __enter__ surfaces a useful error."""
    with pytest.raises(RuntimeError, match="exited prematurely"):
        with RunnerTCPSubprocess(app_factory_path="omnigent.does.not.exist:app"):
            pass


@pytest.mark.asyncio
async def test_tcp_client_connection_error_when_port_unbound() -> None:
    """Pointing the client at an unbound port surfaces ConnectError cleanly."""
    # Use a high port that's almost certainly free.
    async with create_tcp_client("http://127.0.0.1:1") as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("/health")
