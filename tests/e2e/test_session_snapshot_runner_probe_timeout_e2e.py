"""E2E: the session-snapshot live-status probe is bounded and not repeated.

``GET /v1/sessions/{id}`` builds the session snapshot in
``_get_session_snapshot``. On a status-cache miss (server restart, or a freshly
bound session whose relay has not published yet) for a runner-bound session it
asks the runner for live status. For multi-runner deployments that runner
client is an ``httpx.AsyncClient`` over ``WSTunnelTransport``, whose
``handle_async_request`` awaits the response head with no deadline and never
reads httpx timeouts, so the probe hangs unless a real deadline bounds the
await. Unbounded, a runner that is online but slow to answer holds the snapshot
for its full delay; and if a probe that timed out is not remembered, the very
next snapshot repeats it and waits again -- a per-session, per-request penalty
on every load.

This drives the REAL user journey against real processes:

* a real ``omnigent server`` subprocess and a real runner subprocess bound over
  the WebSocket tunnel (the production ``WSTunnelTransport`` code path);
* the runner is made "online but slow": its ``GET /v1/sessions/{id}`` handler
  sleeps well past any probe deadline, then returns a non-200 -- exactly the
  "overloaded host / starved event loop" the report describes (the report's own
  suggested repro is a ``sleep`` in that handler);
* the server is restarted to empty the in-memory status cache (the report's
  step 2), while ``conversations.runner_id`` survives in the DB, so the next
  snapshot takes the empty-cache live-status probe branch;
* the session snapshot is then loaded twice and timed.

Only the runner's slowness is injected; every server-side snapshot / transport
path below is unmodified product code.

On the buggy build both loads hang for the runner's full delay (no deadline
takes effect, and the timed-out probe is repeated) -- the assertions below
fail. On a fixed build the probe is bounded by a real deadline and a timed-out
probe is skipped for a short window, so both loads return well under the
runner's delay -- the assertions pass.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_session_snapshot_runner_probe_timeout_e2e.py -v
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy in the environment; every HTTP call in
# this test targets 127.0.0.1, so bypass proxy autodetection entirely.
_http = httpx.Client(trust_env=False)

_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# How long the runner takes to answer GET /v1/sessions/{id} once armed. Chosen
# well above any reasonable probe deadline, so a build with no effective
# deadline pays the whole delay.
_RUNNER_SLEEP_S = 12.0
# A single snapshot load must complete well under the runner's full delay: a
# real deadline bounds it (a healthy runner answers in tens of ms), so this is
# generous. On the buggy build each load takes ~_RUNNER_SLEEP_S and trips this.
_BOUND_S = 8.0

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0

# Runner bootstrap: wrap ``dispatch_via_asgi`` so that once armed (the arm file
# exists), the runner answers the exact snapshot path GET /v1/sessions/{id} by
# sleeping past any probe deadline and then returning 503 -- an online but
# slow, erroring host. The arm file is only created AFTER the runner is bound,
# so binding / relay traffic runs at full speed. Every other request (init POST,
# /stream, /items, subpaths) delegates to the real runner app unchanged.
_RUNNER_BOOTSTRAP = f"""
import asyncio
import os
import re

import omnigent.runner.transports.ws_tunnel.serve as _serve

_ORIG_DISPATCH = _serve.dispatch_via_asgi
_SNAPSHOT_RE = re.compile(r"^/v1/sessions/[^/]+$")
_ARM_FILE = os.environ["OMNIGENT_TEST_SLOW_PROBE_ARM_FILE"]
_SLEEP_S = {_RUNNER_SLEEP_S!r}


async def _slow_503_app(scope, receive, send):
    await asyncio.sleep(_SLEEP_S)
    await send({{
        "type": "http.response.start",
        "status": 503,
        "headers": [(b"content-type", b"application/json")],
    }})
    await send({{
        "type": "http.response.body",
        "body": b'{{"error": "overloaded"}}',
        "more_body": False,
    }})


async def _dispatch_with_slow_snapshot(app, frame, send_text):
    if (
        frame.method == "GET"
        and _SNAPSHOT_RE.match(frame.path)
        and os.path.exists(_ARM_FILE)
    ):
        await _ORIG_DISPATCH(_slow_503_app, frame, send_text)
        return
    await _ORIG_DISPATCH(app, frame, send_text)


_serve.dispatch_via_asgi = _dispatch_with_slow_snapshot

from omnigent.runner._entry import main

main()
"""


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy in the way."""
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_agent_session(base_url: str) -> str:
    """Create a minimal single-model agent session via multipart POST /v1/sessions."""
    config = {
        "name": "slow-probe-agent",
        "prompt": "you are a test agent",
        "executor": {
            "harness": "claude-sdk",
            "model": "claude-sonnet-4",
            "profile": "test",
        },
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = yaml.dump(config).encode()
            info = tarfile.TarInfo("slow-probe-agent.yaml")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        bundle = buf.getvalue()
    resp = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    resp.raise_for_status()
    return str(resp.json()["session_id"])


def test_session_snapshot_probe_is_bounded_and_not_repeated(tmp_path: Path) -> None:
    """A slow, erroring runner must not make session loads hang past a deadline.

    Journey (the reporter's): a session is bound to a runner over the WS tunnel;
    the runner is online but slow to answer GET /v1/sessions/{id}; the server's
    status cache is cleared (restart). Loading the session then takes the
    empty-cache live-status probe branch. On the fixed build the probe is bounded
    by a real deadline and a timed-out probe is skipped, so both loads return well
    under the runner's delay; on the buggy build both loads hang for the runner's
    full delay (no deadline takes effect, and the probe repeats).

    :param tmp_path: Per-test temp dir (server DB, runner HOME, workspace).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    database_uri = f"sqlite:///{tmp_path / 'chat.db'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    runner_log_file = tmp_path / "runner-process.log"
    arm_file = tmp_path / "arm-slow-probe"

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    server_log = (tmp_path / "server.log").open("w")
    runner_stdout = (tmp_path / "runner.stdout.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None

    def _runner_log() -> str:
        return runner_log_file.read_text() if runner_log_file.exists() else ""

    def _spawn_server() -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({"OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
        return proc

    def _wait_runner_online() -> bool:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                status = _http.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
                if status.status_code == 200 and status.json().get("online") is True:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_S)
        return False

    try:
        server_proc = _spawn_server()

        runner_proc = subprocess.Popen(
            [sys.executable, "-c", _RUNNER_BOOTSTRAP],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    "HOME": str(runner_home),
                    "OMNIGENT_PROCESS_LOG_FILE": str(runner_log_file),
                    "OMNIGENT_LOG_LEVEL": "INFO",
                    "OMNIGENT_TEST_SLOW_PROBE_ARM_FILE": str(arm_file),
                }
            ),
            stdout=runner_stdout,
            stderr=subprocess.STDOUT,
        )
        assert _wait_runner_online(), f"runner never came online; log:\n{_runner_log()[-3000:]}"

        session_id = _create_agent_session(base_url)

        # Bind the session to the runner (fast: the slow probe is not yet armed).
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=30.0,
        ).raise_for_status()
        # Let the bind's init POST / relay handshake settle.
        time.sleep(3.0)

        # Arm the slow, erroring snapshot probe on the runner.
        arm_file.write_text("armed")

        # Reporter step 2: clear the server's in-memory status cache by
        # restarting the server. conversations.runner_id survives in the DB, so
        # the next snapshot takes the empty-cache live-status probe branch. The
        # runner reconnects the tunnel to the fresh server process.
        _terminate(server_proc)
        server_proc = _spawn_server()
        assert _wait_runner_online(), (
            f"runner never reconnected after restart; log:\n{_runner_log()[-3000:]}"
        )
        time.sleep(1.0)

        # First session load: on an empty cache the snapshot probes the runner.
        start = time.monotonic()
        first = _http.get(
            f"{base_url}/v1/sessions/{session_id}?include_items=false",
            timeout=_RUNNER_SLEEP_S + 30.0,
        )
        first_elapsed = time.monotonic() - start
        first.raise_for_status()

        # Second load: on a fixed build the prior failed probe is skipped for a
        # short window (or is at worst bounded again), so this does not pay the
        # runner's full delay a second time.
        start = time.monotonic()
        second = _http.get(
            f"{base_url}/v1/sessions/{session_id}?include_items=false",
            timeout=_RUNNER_SLEEP_S + 30.0,
        )
        second_elapsed = time.monotonic() - start
        second.raise_for_status()

        assert first_elapsed < _BOUND_S, (
            "session snapshot live-status probe was not bounded by a real deadline: "
            f"GET /v1/sessions/{{id}} took {first_elapsed:.1f}s against a runner that "
            f"answered in {_RUNNER_SLEEP_S:.0f}s."
        )
        assert second_elapsed < _BOUND_S, (
            "a failed live-status probe was repeated on the next snapshot: the second "
            f"GET /v1/sessions/{{id}} took {second_elapsed:.1f}s (the runner's full "
            f"{_RUNNER_SLEEP_S:.0f}s delay), so the failed probe was not remembered / "
            "skipped for a window."
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_stdout.close()
