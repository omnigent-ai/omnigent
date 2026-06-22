"""End-to-end UDS transport tests — the load-bearing Phase 2 assertion.

A real uvicorn subprocess. A real Unix socket. A real httpx client.
A real GET /health round-trip. If this passes, the Phase 2 transport
is wired up correctly and Phase 3 / 4 just swap the wire bytes.

These tests skip rather than fail on platforms without UDS support
(Windows). Linux/macOS run them as the real round-trip they're
intended to be.
"""

from __future__ import annotations

import os
import sys
import tempfile

import httpx
import pytest

from omnigent.runner.transports.uds import (
    RunnerSubprocess,
    create_uds_client,
)

# UDS isn't available on Windows. Tests use this in a skipif guard
# rather than declaring the whole module skipped — the function-
# level marker means each test's skip reason shows up.
_REQUIRES_UDS = pytest.mark.skipif(
    sys.platform == "win32", reason="Unix domain sockets are POSIX-only"
)

# ``_entry:create_app`` requires RUNNER_SERVER_URL in the subprocess
# environment. Tests don't need a real Omnigent server — the env var only
# needs to satisfy the non-empty check so the factory can build the
# httpx client. The actual URL is never dialled during these transport
# smoke tests.
_FAKE_SERVER_ENV = {"RUNNER_SERVER_URL": "http://127.0.0.1:1"}


# ── Subprocess lifecycle ─────────────────────────────────


@_REQUIRES_UDS
def test_runner_subprocess_starts_and_binds_socket() -> None:
    """Context manager launches uvicorn and exits with socket bound.

    The bind itself is the load-bearing assertion: if the subprocess
    didn't reach uvicorn's startup-complete state, the socket
    wouldn't be listening, and ``RunnerSubprocess.__enter__`` would
    have raised TimeoutError.
    """
    with RunnerSubprocess(extra_env=_FAKE_SERVER_ENV) as runner:
        assert runner.socket_path is not None
        assert os.path.exists(runner.socket_path), (
            "Socket file must exist after __enter__ returned — "
            "uvicorn's startup-complete check is what blocks the "
            "context manager from yielding control prematurely."
        )


@_REQUIRES_UDS
def test_runner_subprocess_cleans_up_on_exit() -> None:
    """After __exit__, the subprocess is gone and the socket is removed.

    Without cleanup, repeated tests would leak socket files into
    /tmp and zombie uvicorn processes. The context manager owns the
    full lifecycle — entry spawns, exit reaps.
    """
    with RunnerSubprocess(extra_env=_FAKE_SERVER_ENV) as runner:
        socket_path = runner.socket_path
        process = runner._process
        assert process is not None
        assert process.poll() is None, "subprocess should be alive in scope"
    # After exit:
    # - Process must have terminated.
    assert process.poll() is not None, (
        "Subprocess must be reaped on context-manager exit; otherwise "
        "test runs leak uvicorn processes."
    )
    # - Socket file is gone (TemporaryDirectory cleaned up).
    assert not os.path.exists(socket_path)


@_REQUIRES_UDS
def test_runner_subprocess_with_explicit_socket_path() -> None:
    """Caller-supplied socket path is honored.

    Uses a short tempdir under /tmp instead of pytest's tmp_path —
    macOS limits sun_path to 104 bytes and pytest's per-test temp
    path on darwin already exceeds that before any filename is added.
    """
    with tempfile.TemporaryDirectory(prefix="oa-uds-", dir="/tmp") as tdir:
        sock = os.path.join(tdir, "mine.sock")
        with RunnerSubprocess(socket_path=sock, extra_env=_FAKE_SERVER_ENV) as runner:
            assert runner.socket_path == sock
            assert os.path.exists(sock)


# ── Round-trip via the UDS client ────────────────────────


@_REQUIRES_UDS
@pytest.mark.asyncio
async def test_health_round_trip_via_uds() -> None:
    """The flagship Phase 2 test: server → UDS → uvicorn → runner FastAPI.

    A successful 200 from /health proves every layer is wired:
    1. uvicorn imported and started the runner FastAPI app.
    2. uvicorn bound a UDS at the agreed path.
    3. ``create_uds_client`` constructed an httpx AsyncClient that
       knows how to dial a UDS.
    4. The httpx UDS transport correctly routed the GET to the
       socket.
    5. The runner's /health handler ran and returned ``{"status": "ok"}``.

    A failure here means one of those layers is broken; the
    subsequent tests would be unreliable until this is fixed.
    """
    with RunnerSubprocess(extra_env=_FAKE_SERVER_ENV) as runner:
        assert runner.socket_path is not None
        async with create_uds_client(runner.socket_path) as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}


@_REQUIRES_UDS
@pytest.mark.asyncio
async def test_post_session_events_stub_via_uds() -> None:
    """Session events endpoint is reachable via UDS.

    ``create_runner_app_from_env`` starts without a HarnessProcessManager
    (scaffold mode), so the endpoint returns 501. The test asserts that
    the route exists and the transport delivers the response — not that
    the runner is fully functional.
    """
    with RunnerSubprocess(extra_env=_FAKE_SERVER_ENV) as runner:
        assert runner.socket_path is not None
        async with create_uds_client(runner.socket_path) as client:
            response = await client.post(
                "/v1/sessions/conv_test/events",
                json={"type": "message", "role": "user", "content": []},
            )
            assert response.status_code == 501


# ── Failure modes ────────────────────────────────────────


@_REQUIRES_UDS
def test_subprocess_with_bad_app_factory_raises() -> None:
    """Bad import path → uvicorn crashes → __enter__ surfaces a useful error.

    The error message MUST include enough detail (rc + stderr) for
    the developer to diagnose. A bare TimeoutError would be
    misleading — the subprocess died, it didn't time out.
    """
    with pytest.raises(RuntimeError, match="exited prematurely"):
        with RunnerSubprocess(app_factory_path="omnigent.does.not.exist:app"):
            pass


@_REQUIRES_UDS
@pytest.mark.asyncio
async def test_uds_client_connection_error_when_socket_missing(tmp_path) -> None:
    """Pointing the client at a socket that isn't bound raises a clean ConnectError.

    Documents the failure mode for the case where the server
    dispatches before the runner is up.
    """
    fake_socket = str(tmp_path / "no-such-socket.sock")
    async with create_uds_client(fake_socket) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("/health")
