"""E2E: SIGTERM must shut the runner down gracefully while a native turn's
MCP tool-list notification is still waiting on a bridge that never appears.

On a native-CLI harness turn the runner schedules a background
``notifications/tools/list_changed``
via ``_ensure_comment_relay_started`` -> ``post_tools_changed`` ->
``_wait_for_server_info``. That readiness probe is a *synchronous* 30s poll
(``_TOOLS_CHANGED_READY_TIMEOUT_S``) running in the loop's default executor,
waiting for the bridge's ``server.json`` control-endpoint file. For a
non-claude native harness (here ``hermes``) nothing ever writes that file into
the harness bridge dir, so the poll runs its full 30s.

When SIGTERM arrives mid-poll the runner cancels the async notification task,
but cancelling the task does not stop the synchronous poll already running in
the worker thread. ``asyncio.run``'s teardown then blocks in
``shutdown_default_executor`` joining that worker, so the runner stays alive
long past its graceful-shutdown window and only a SIGKILL stops it.

Journey:

1. bring up a runner bound to a server, with a ``hermes`` native session,
2. send the first chat turn so the runner schedules the tool-list
   notification, whose readiness poll blocks on a ``server.json`` that never
   appears,
3. SIGTERM the runner while that poll is in flight,
4. the runner must exit gracefully within the shutdown budget.

While the bug is live step 4 fails: the runner is still alive at the budget
and has to be SIGKILLed. After a fix, cancelling the notification stops its
readiness wait and the runner exits promptly.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# The stub hermes CLI must report it is mid-turn (first turn also pays the
# harness-wrap subprocess boot) before we SIGTERM.
_TURN_START_TIMEOUT_S = 90.0
# A clean runner shutdown closes its tunnel and lifespan in a couple of
# seconds. The buggy path blocks in shutdown_default_executor for what is left
# of the 30s readiness poll (~25s+ here), so this budget cleanly separates the
# two while giving a healthy shutdown ample slack on slow CI.
_SHUTDOWN_BUDGET_S = 15.0

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars that
# must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))

# Ambient runner/host/zygote env that would otherwise leak into the spawned
# runner and push it onto the zygote-fork path (it would block on control FDs
# it does not have), when this test itself runs inside a server-spawned runner.
_LEAKED_RUNNER_ENV = (
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_RUNNER_PARENT_PID",
    "OMNIGENT_RUNNER_ISOLATE_SESSION",
    "OMNIGENT_RUNNER_WORKSPACE",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
    "OMNIGENT_HOST_NAME",
    "RUNNER_SERVER_URL",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_PROCESS_LOG_FILE",
    "OMNIGENT_DATA_DIR",
)

_HERMES_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Stub Hermes CLI: mimics `hermes chat -q <msg> -Q --source tool` — prints
    # the session_id line the executor parses, records its PID, then "works"
    # for a long time so the turn stays in flight while we SIGTERM the runner.
    set -u
    STATE_DIR="${HERMES_STUB_STATE_DIR:?}"
    echo "session_id: stub_hermes_session"
    echo "$$" > "${STATE_DIR}/hermes.pid"
    trap 'exit 143' TERM INT
    for _ in $(seq 1 180); do
      sleep 1
    done
    echo "finished the long task"
    """
)


def _clean_child_env(extra: dict[str, str]) -> dict[str, str]:
    """os.environ minus the leaked runner/host/zygote vars, plus *extra*."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _LEAKED_RUNNER_ENV and not key.startswith("OMNIGENT_RUNNER_ZYGOTE")
    }
    for var in ("NO_PROXY", "no_proxy"):
        env[var] = ",".join(filter(None, [env.get(var, ""), "127.0.0.1,localhost"]))
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env.update(extra)
    return env


def _create_hermes_session(base_url: str, runner_id: str) -> str:
    """Create a session on an inline ``hermes`` native agent bound to the runner."""
    agent_name = "shutdown_hang_hermes"
    agent_yaml = (
        f"name: {agent_name}\n"
        f"prompt: You are a test agent for the runner-shutdown reproduction.\n"
        f"executor:\n"
        f"  model: stub-model\n"
        f"  harness: hermes\n"
    )
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = agent_yaml.encode()
            info = tarfile.TarInfo(f"{agent_name}.yaml")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        bundle = buf.getvalue()

    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]

    patch = _client.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()
    return session_id


@pytest.fixture
def hermes_runner_rig(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, subprocess.Popen[bytes], str, Path]]:
    """Spawn an isolated server + runner whose ``hermes`` CLI is a local stub.

    Yields ``(base_url, runner_proc, runner_id, state_dir)``. The runner is
    returned so the test can drive its SIGTERM shutdown directly; teardown is a
    best-effort SIGKILL backstop.
    """
    work = tmp_path_factory.mktemp("runner_shutdown_hang")
    config_home = work / "config-home"
    home_dir = work / "home"
    state_dir = work / "hermes-stub-state"
    artifacts = work / "artifacts"
    bin_dir = work / "bin"
    for path in (config_home, home_dir, state_dir, artifacts, bin_dir):
        path.mkdir(parents=True, exist_ok=True)

    hermes_stub = bin_dir / "hermes"
    hermes_stub.write_text(_HERMES_STUB)
    hermes_stub.chmod(0o755)

    with __import__("socket").socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared = {
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "HOME": str(home_dir),
        "OMNIGENT_DATA_DIR": str(work / "data"),
    }
    server_env = _clean_child_env({**shared, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token})
    runner_env = _clean_child_env(
        {
            **shared,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            "OMNIGENT_HERMES_PATH": str(hermes_stub),
            "HERMES_STUB_STATE_DIR": str(state_dir),
        }
    )

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
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
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "hermes runner rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        yield (base_url, runner_proc, runner_id, state_dir)
    finally:
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


def _wait_for(predicate, timeout_s: float, interval_s: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


@pytest.mark.timeout(300)
def test_runner_shuts_down_gracefully_while_tools_changed_notify_pending(
    hermes_runner_rig: tuple[str, subprocess.Popen[bytes], str, Path],
) -> None:
    """SIGTERM must not hang the runner behind a pending readiness poll."""
    base_url, runner_proc, runner_id, state_dir = hermes_runner_rig
    pid_file = state_dir / "hermes.pid"

    session_id = _create_hermes_session(base_url, runner_id)

    body = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "Please do a long-running task."}],
        },
    }
    events = _client.post(f"{base_url}/v1/sessions/{session_id}/events", json=body, timeout=30.0)
    events.raise_for_status()

    # The turn is genuinely in flight (and the tool-list notification therefore
    # scheduled and mid-poll) once the stub CLI reports its PID.
    assert _wait_for(pid_file.exists, _TURN_START_TIMEOUT_S), (
        f"stub hermes CLI never started a turn within {_TURN_START_TIMEOUT_S:.0f}s — "
        "check the harness wrap booted (OMNIGENT_HERMES_PATH resolution / runner log)"
    )

    # SIGTERM while the readiness poll is in flight, then time the exit.
    runner_proc.send_signal(signal.SIGTERM)
    sent_at = time.monotonic()
    with contextlib.suppress(subprocess.TimeoutExpired):
        runner_proc.wait(timeout=_SHUTDOWN_BUDGET_S)
    elapsed = time.monotonic() - sent_at

    exited = runner_proc.poll() is not None
    if not exited:
        runner_proc.kill()
        runner_proc.wait(timeout=10)

    assert exited, (
        f"runner did not exit within {_SHUTDOWN_BUDGET_S:.0f}s of SIGTERM "
        f"(waited {elapsed:.1f}s, then SIGKILLed): shutdown blocked in "
        "shutdown_default_executor joining the synchronous tools-changed "
        "readiness poll (post_tools_changed -> _wait_for_server_info)."
    )
