"""End-to-end: a headless codex-native sub-agent must not die in the startup timeout.

Polly's bundled codex sub-agent (``examples/polly/agents/codex/config.yaml``,
``executor.config.harness: codex-native``) is dispatched headlessly — no TTY,
nobody at a terminal. On a machine whose native-Codex launch routing resolves
to "Codex CLI login" without a usable stored credential, the runner's
``--remote`` Codex TUI parks on the ChatGPT sign-in / onboarding screen and
never emits ``thread/started``. The sub-agent's first turn then burns the
30s ``wait_for_thread_started`` startup timeout and dies with::

    inner executor error: Codex native thread never started: Codex
    app-server never started a thread (startup timed out: TimeoutError). ...

so the cross-vendor review never runs (runner log: "Codex TUI never started
a thread for conv_...; chat will not forward").

This drives the real stack — server subprocess, real
``omnigent.runner._entry`` runner, real codex CLI — through the exact
sub-agent shape Polly ships (a spec with ``harness: codex-native`` +
``yolo: true``) and asserts the first turn does NOT die in the thread-start
timeout. While the bug is live the test fails on that timeout error; after a
fix the turn must either start a thread or fail fast with a clear
non-timeout error (e.g. a refusal to launch headlessly without a routable
credential)::

    .venv/bin/python -m pytest tests/e2e/test_codex_native_headless_subagent_e2e.py -v
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Polly's codex sub-agent shape (examples/polly/agents/codex/config.yaml),
# reduced to the fields that pick the launch path under test.
_CODEX_SUBAGENT_YAML = """\
spec_version: 1
name: codex
description: Codex coding sub-agent (Polly cross-vendor reviewer shape).

executor:
  type: omnigent
  config:
    harness: codex-native
    yolo: true

prompt: |
  You are Codex, a coding sub-agent dispatched for a single scoped REVIEW
  task. Judge the given diff against its acceptance contract.

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

_HEALTH_TIMEOUT_S = 60.0
# The buggy path takes the 30s thread-start timeout plus the executor's
# bridge-state poll before the turn errors; leave generous headroom for the
# fixed path's real turn as well.
_TURN_OUTCOME_TIMEOUT_S = 150.0
_POLL_INTERVAL_S = 3.0

_STARTUP_TIMEOUT_MARKER = "startup timed out"
_THREAD_NEVER_STARTED_MARKER = "never started a thread"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Shared fixtures/helpers (e.g. the conftest session factory) use ambient
# ``httpx`` calls that DO trust env, so also exclude loopback from any forced
# proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback excluded from any forced HTTP(S) proxy."""
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


@pytest.fixture
def credential_less_codex_rig(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """Server + runner whose environment has no routable Codex credential.

    Empty ``CODEX_HOME`` (Codex not logged in) and empty
    ``OMNIGENT_CONFIG_HOME`` (no provider configured for the codex harness):
    the launch router resolves to "Codex CLI login" with nothing to show at
    a headless terminal — the routing state in which the reported timeout
    fires.

    :returns: ``(base_url, runner_id, runner_log_path)``.
    """
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is required for the codex-native headless repro")

    from omnigent.runner.identity import token_bound_runner_id

    work = tmp_path_factory.mktemp("codex_headless_subagent")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(codex_home),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

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
                "credential-less codex rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )
        yield (base_url, runner_id, runner_log)
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


def _spec_bundle() -> bytes:
    """Gzip the codex sub-agent spec as a session bundle (strict parser path)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CODEX_SUBAGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.timeout(400)
def test_polly_shaped_codex_subagent_first_turn_survives_headless_dispatch(
    credential_less_codex_rig: tuple[str, str, Path],
) -> None:
    """A headless codex-native sub-agent turn must not die in the startup timeout.

    Journey: create a session from Polly's codex sub-agent spec, bind it to
    the runner (headless — no TTY anywhere), send the review prompt, and
    wait for a terminal outcome. While the bug is live the turn errors with
    the ``startup timed out`` thread-start failure after ~30s; the test
    fails on exactly that marker.
    """
    base_url, runner_id, runner_log = credential_less_codex_rig

    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(_REPO_ROOT)})},
        files={"bundle": ("codex.tar.gz", _spec_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    try:
        bind = _client.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=60.0,
        )
        bind.raise_for_status()

        send = _client.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "REVIEW: judge this (empty) diff against the contract.",
                        }
                    ],
                },
            },
            timeout=30.0,
        )
        assert send.status_code == 202, f"send rejected: {send.status_code} {send.text}"

        # Wait for a terminal outcome: an error item, an assistant message,
        # or the runner stamping the Codex thread id (thread started).
        error_messages: list[str] = []
        thread_started = False
        assistant_replied = False
        deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
        while time.monotonic() < deadline:
            items = _client.get(
                f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0
            )
            if items.status_code == 200:
                data = items.json()["data"]
                error_messages = [
                    str(item.get("message", "")) for item in data if item.get("type") == "error"
                ]
                assistant_replied = any(
                    item.get("type") == "message" and item.get("role") == "assistant"
                    for item in data
                )
            session = _client.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
            if session.status_code == 200 and session.json().get("external_session_id"):
                thread_started = True
            if error_messages or thread_started or assistant_replied:
                break
            time.sleep(_POLL_INTERVAL_S)

        timed_out_errors = [
            message
            for message in error_messages
            if _STARTUP_TIMEOUT_MARKER in message or _THREAD_NEVER_STARTED_MARKER in message
        ]
        assert not timed_out_errors, (
            "headless codex-native sub-agent turn died in the thread-start "
            f"timeout (the reported headless failure): {timed_out_errors[0][:500]}\n"
            f"runner log tail:\n{runner_log.read_text()[-1500:]}"
        )
        assert thread_started or assistant_replied or error_messages, (
            "turn reached no terminal outcome within "
            f"{_TURN_OUTCOME_TIMEOUT_S:.0f}s (no thread, no reply, no error)"
        )
    finally:
        _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
