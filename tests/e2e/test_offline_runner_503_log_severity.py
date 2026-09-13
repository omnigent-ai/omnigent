"""E2E: an offline-runner resource 503 must not be logged as ERROR + traceback.

A runner going offline (host reboot, idle-reap, tunnel drop) is a normal
operational state. When a session's runner is offline, every runner-proxied
resource GET (environment, filesystem, terminals) correctly returns 503
``runner_unavailable`` — but the server's ``OmnigentError`` handler logs each
one via its ``http_status >= 500`` band as
``ERROR [server.app] Internal error: runner … is offline …`` with a full stack
trace, burying genuine ERRORs under per-hit tracebacks.

This drives the real user journey end to end against a spawned server: bind a
session to a live runner, SIGKILL the runner (the "host rebooted" moment), hit
a resource endpoint the session-open fan-out uses, and assert the correct 503
is returned while the server's application log records NO ERROR-level entry
and NO traceback for that expected transient condition.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import token_bound_runner_id

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HELLO_WORLD_YAML = """\
name: hello_world
prompt: You are a friendly assistant.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

_BOOT_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 1.0
_OFFLINE_TIMEOUT_S = 15.0


def _find_free_port() -> int:
    """Pick a free TCP port on localhost.

    :returns: An OS-assigned free port number.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _subprocess_env(extra: dict[str, str]) -> dict[str, str]:
    """Build a clean env for a spawned server/runner subprocess.

    Strips any ambient runner/host identity vars (they leak in when the test
    itself runs inside a server-spawned runner and would make the child take
    the wrong startup path), prepends the worktree and the client SDK to
    ``PYTHONPATH`` so the subprocess imports the code under test, and merges
    ``extra`` on top.

    :param extra: Vars to set for this subprocess.
    :returns: The merged environment mapping.
    """
    env = {**os.environ}
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")) or key in (
            "RUNNER_SERVER_URL",
            "OMNIGENT_REMOTE_AUTH_TOKEN",
            # A leaked parent log path would redirect the spawned server's
            # application log away from OMNIGENT_DATA_DIR, which this test
            # reads to assert log severity.
            "OMNIGENT_PROCESS_LOG_FILE",
        ):
            env.pop(key)
    pythonpath = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["PYTHONPATH"] = pythonpath.rstrip(os.pathsep)
    env.update(extra)
    return env


@pytest.fixture
def offline_runner_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """Spawn a server + runner, bind a session, then SIGKILL the runner.

    Self-contained on purpose: the shared session-scoped ``live_server``
    fixture must keep its runner alive for other tests, and this test also
    needs the server's application-log file at a known location
    (``OMNIGENT_DATA_DIR``) to assert on log severity.

    :param tmp_path_factory: Pytest temp factory for the DB, artifacts,
        data dir, and process logs.
    :returns: Yields ``(base_url, session_id, app_log_dir)`` with the
        session's runner already offline.
    """
    work = tmp_path_factory.mktemp("offline_503_log")
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    agent_yaml = work / "hello_world.yaml"
    agent_yaml.write_text(_HELLO_WORLD_YAML)
    data_dir = work / "data"

    server_log = open(work / "server.log", "w")  # noqa: SIM115 — lives for Popen lifetime; closed in teardown
    server = subprocess.Popen(
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
            f"sqlite:///{work / 'test.db'}",
            "--artifact-location",
            str(work / "artifacts"),
            "--agent",
            str(agent_yaml),
        ],
        env=_subprocess_env(
            {
                "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
                "OMNIGENT_DATA_DIR": str(data_dir),
                # No LLM turn is ever driven; a dead base URL keeps any
                # accidental provider call from reaching the network.
                "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
                "OPENAI_API_KEY": "test-key",
            }
        ),
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )

    runner_log = open(work / "runner.log", "w")  # noqa: SIM115 — lives for Popen lifetime; closed in teardown
    runner = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=_subprocess_env(
            {
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": base_url,
                "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
                "OPENAI_API_KEY": "test-key",
            }
        ),
        stdout=runner_log,
        stderr=subprocess.STDOUT,
    )

    client = httpx.Client(timeout=5.0, trust_env=False)
    try:
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(
                    f"server exited early (code {server.returncode}); "
                    f"log:\n{(work / 'server.log').read_text()[-3000:]}"
                )
            try:
                resp = client.get(f"{base_url}/v1/runners/{runner_id}/status")
                if resp.status_code == 200 and resp.json().get("online") is True:
                    online = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_INTERVAL_S)
        if not online:
            raise RuntimeError(
                f"runner did not come online within {_BOOT_TIMEOUT_S:.0f}s; "
                f"runner log:\n{(work / 'runner.log').read_text()[-3000:]}"
            )

        agents = client.get(f"{base_url}/v1/agents").json()
        rows = agents.get("data", agents if isinstance(agents, list) else [])
        agent_id = next(
            row.get("agent_id") or row.get("id")
            for row in rows
            if row.get("name") == "hello_world"
        )
        created = client.post(f"{base_url}/v1/sessions", json={"agent_id": agent_id})
        created.raise_for_status()
        payload = created.json()
        session_id = payload.get("session_id") or payload["id"]
        client.patch(
            f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id}
        ).raise_for_status()

        # The "host rebooted / runner idle-reaped" moment: SIGKILL, then wait
        # for the server to notice the tunnel drop and report offline.
        runner.send_signal(signal.SIGKILL)
        runner.wait(timeout=10)
        deadline = time.monotonic() + _OFFLINE_TIMEOUT_S
        offline = False
        while time.monotonic() < deadline:
            health = client.get(f"{base_url}/health", params={"session_id": session_id}).json()
            if health.get("session", {}).get("runner_online") is False:
                offline = True
                break
            time.sleep(0.5)
        assert offline, "server never reported the killed runner offline"

        yield base_url, session_id, data_dir / "logs" / "server"
    finally:
        client.close()
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        runner_log.close()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server_log.close()


def test_offline_runner_resource_503_not_logged_as_error(
    offline_runner_session: tuple[str, str, Path],
) -> None:
    """A resource GET on an offline-runner session 503s without an ERROR log.

    Drives the exact request the session-open fan-out sends
    (``GET /v1/sessions/{id}/resources/environments/default``) against a
    session whose runner just died, then reads the server's application log.

    The 503 ``runner_unavailable`` response is the *correct* contract and must
    stay. What must NOT happen is the server treating this expected transient
    state as an internal error: no ``ERROR``-level "Internal error:" entry and
    no stack trace for the offline-runner condition.

    :param offline_runner_session: ``(base_url, session_id, app_log_dir)``
        with the session's runner already offline.
    """
    base_url, session_id, app_log_dir = offline_runner_session
    client = httpx.Client(timeout=10.0, trust_env=False)
    try:
        resp = client.get(f"{base_url}/v1/sessions/{session_id}/resources/environments/default")
    finally:
        client.close()

    # The response contract is already correct and must not regress.
    assert resp.status_code == 503, (
        f"expected 503 for an offline-runner resource GET, got {resp.status_code}: "
        f"{resp.text[:500]}"
    )
    body = json.loads(resp.text)
    assert body["error"]["code"] == "runner_unavailable", body

    # Give the log handler a moment to flush the record for that request.
    time.sleep(1.0)
    log_files = sorted(app_log_dir.glob("server-*.log"))
    assert log_files, f"no server application log found under {app_log_dir}"
    log_text = "\n".join(path.read_text() for path in log_files)
    assert "is offline for conversation" in log_text, (
        "expected the server log to mention the offline runner at all — "
        "did the request reach the runner router?"
    )

    error_lines = [
        line
        for line in log_text.splitlines()
        if "ERROR" in line and "Internal error" in line and "is offline for conversation" in line
    ]
    traceback_hit = "omnigent.errors.OmnigentError: runner" in log_text
    assert not error_lines and not traceback_hit, (
        "an expected offline-runner 503 (runner_unavailable) was logged as an "
        "internal error: found "
        f"{len(error_lines)} ERROR 'Internal error' line(s) and "
        f"traceback={'yes' if traceback_hit else 'no'} in the server log. "
        "A runner being offline is a normal operational state and should be "
        "logged at INFO/WARNING without a stack trace. First offending line: "
        f"{error_lines[0] if error_lines else '(traceback only)'}"
    )
