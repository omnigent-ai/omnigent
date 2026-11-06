"""Keep runner-owned services bounded when its tunnel fails before connecting."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from omnigent.runner.identity import (
    RUNNER_AUTH_REJECTED_EXIT_CODE,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    token_bound_runner_id,
)
from tests._helpers.live_server import isolated_process_env

pytestmark = pytest.mark.posix_only


@pytest.fixture
def runner_env(tmp_path: Path) -> dict[str, str]:
    # Each runner owns process-wide signal handlers, GC freezing and its executor.
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    return isolated_process_env(
        home,
        {
            "OMNIGENT_CONFIG_HOME": str(home / "config"),
            "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
            "OMNIGENT_RUNNER_ID": token_bound_runner_id("synthetic-binding"),
            "OMNIGENT_RUNNER_HOST_OWNS_GLOBAL_CLEANUP": "1",
            PROCESS_LOG_FILE_ENV_VAR: str(tmp_path / "runner.log"),
            "RUNNER_SERVER_URL": "http://127.0.0.1:1",
        },
        base_env={
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("RUNNER_", "OTEL_"))
        },
    )


class _RejectUpgrade(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def rejecting_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RejectUpgrade)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("bootstrap_bearer", [False, True])
def test_real_401_exit_keeps_reason_without_lifespan_traceback(
    runner_env: dict[str, str], rejecting_server: str, bootstrap_bearer: bool
) -> None:
    runner_env["RUNNER_SERVER_URL"] = rejecting_server
    if bootstrap_bearer:
        runner_env[RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR] = "synthetic-bootstrap-token"
    completed = subprocess.run(
        [sys.executable, "-P", "-m", "omnigent.runner._entry"],
        env=runner_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = (
        completed.stdout
        + completed.stderr
        + Path(runner_env[PROCESS_LOG_FILE_ENV_VAR]).read_text()
    )
    assert completed.returncode == RUNNER_AUTH_REJECTED_EXIT_CODE, output
    assert "runner exiting: runner tunnel rejected by server" in output, output
    assert "HTTP 401 persisted across 3" in output, output
    if bootstrap_bearer:
        assert "no SDK/OIDC credential is available to renew it" in output, output
    assert "Traceback (most recent call last)" not in output, output
    assert "CancelledError" not in output, output


_LIFECYCLE_PROBE = r"""
import asyncio
import contextlib
import json
import sys

import omnigent.runner._entry as entry
import omnigent.runner.direct_attach as direct
import omnigent.runner.transports.ws_tunnel.serve as transport

mode, fail_teardown = sys.argv[1], sys.argv[2] == "1"
state = {"lifespan_exited": False, "listener_started": False}
contexts, listeners = [], []
create_app = entry.create_app
start_listener = direct.start_direct_attach_listener

class CleanupError(Exception):
    pass

class ObservedLifespan:
    def __init__(self, real):
        self.real = real
        self.closed = False

    async def __aenter__(self):
        return await self.real.__aenter__()

    async def __aexit__(self, *args):
        result = await self.real.__aexit__(*args)
        self.closed = True
        state["lifespan_exited"] = True
        if fail_teardown:
            raise CleanupError("synthetic lifespan exit failure")
        return result

def observe_app(*args, **kwargs):
    app = create_app(*args, **kwargs)
    real_lifespan = app.router.lifespan_context
    def observed(app):
        cm = ObservedLifespan(real_lifespan(app))
        contexts.append(cm)
        return cm
    app.router.lifespan_context = observed
    return app

async def observe_listener(app):
    listener = await start_listener(app)
    if listener is not None:
        listeners.append(listener)
        state["listener_started"] = True
    return listener

async def tunnel(*args, **kwargs):
    # Only the external tunnel transport is replaced; both apps/lifespans are real.
    if mode == "auth":
        raise transport.RunnerTunnelAuthRejectedError("synthetic auth rejection")
    if mode == "transport":
        raise RuntimeError("synthetic tunnel failure")
    if mode == "idle_failure":
        await asyncio.Event().wait()

async def failed_idle_monitor(**kwargs):
    raise ValueError("synthetic idle monitor failure")

entry.create_app = observe_app
direct.start_direct_attach_listener = observe_listener
transport.serve_tunnel = tunnel
if mode == "idle_failure":
    entry._run_inactivity_monitor = failed_idle_monitor

async def probe():
    try:
        await entry._run_tunnel_from_env()
    except Exception as exc:
        state["error_type"] = type(exc).__name__
        state["error"] = str(exc)
    else:
        state["error_type"] = None
    state["listener_stopped"] = bool(listeners) and all(x._task.done() for x in listeners)
    state["pending_tasks"] = sorted(
        task.get_name() for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    )
    print("RUNNER_CLEANUP_PROBE=" + json.dumps(state), flush=True)
    # Keep a red regression from leaking the resources it just measured.
    for listener in listeners:
        await listener.stop()
    for cm in contexts:
        if not cm.closed:
            with contextlib.suppress(CleanupError):
                await cm.__aexit__(None, None, None)

asyncio.run(probe())
"""


@pytest.mark.parametrize("mode", ["auth", "transport", "idle_failure", "closed"])
@pytest.mark.parametrize("fail_teardown", [False, True])
def test_real_runner_closes_owned_services_and_preserves_failure(
    runner_env: dict[str, str], mode: str, fail_teardown: bool
) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _LIFECYCLE_PROBE, mode, str(int(fail_teardown))],
        env=runner_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    (line,) = (
        line for line in completed.stdout.splitlines() if line.startswith("RUNNER_CLEANUP_PROBE=")
    )
    state = json.loads(line.partition("=")[2])
    assert state["listener_started"], output
    assert state["listener_stopped"], output
    assert state["lifespan_exited"], output
    assert state["pending_tasks"] == [], output
    expected_error = {
        "auth": "RunnerTunnelAuthRejectedError",
        "transport": "RuntimeError",
        "idle_failure": "ValueError",
        "closed": "CleanupError" if fail_teardown else None,
    }[mode]
    assert state["error_type"] == expected_error, output
    if mode != "closed":
        assert (
            state["error"]
            == {
                "auth": "synthetic auth rejection",
                "transport": "synthetic tunnel failure",
                "idle_failure": "synthetic idle monitor failure",
            }[mode]
        ), output
    if fail_teardown and mode != "closed":
        assert "runner teardown failed while exiting" in output, output
