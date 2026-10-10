"""Answering an orphaned sub-agent prompt after restart clears "Needs response".

A sub-agent's runner dies (SIGTERM / exit 143) and never reconnects, then the
server restarts against the same durable store: the in-memory pending index is
gone but the persisted count survives. This drives that over real server and
runner processes and asserts that answering the restarted session returns
``pending_elicitations_count`` to 0 so the sidebar badge clears.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_server_env, server_executable
from tests._helpers.live_server import terminate_process
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _build_hello_world_bundle,
    _find_free_port,
)

# Patch the presence leave-grace inside the spawned interpreter (a test-process
# monkeypatch can't reach a subprocess), mirroring the live_server fixture.
_SERVER_BOOT = (
    "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
    "from omnigent.cli import main; main()"
)


def _server_command(port: int, db_path: Path, artifact_dir: Path, agent_yaml: Path) -> list[str]:
    """Argv for one server generation bound to the durable SQLite store."""
    return [
        server_executable(),
        "-c",
        _SERVER_BOOT,
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
        "--agent",
        str(agent_yaml),
    ]


def _wait_health(base_url: str, proc: subprocess.Popen, *, runner_id: str | None) -> None:
    """Poll ``/health`` until ready; also require the runner online when given."""
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    last = "not polled"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code == 200:
                if runner_id is None:
                    return
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if status.status_code == 200 and status.json().get("online") is True:
                    return
                last = f"runner status {status.status_code}: {status.text[:120]}"
            else:
                last = f"health HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            # Any transport failure during a startup/restart window is retryable.
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    raise RuntimeError(f"server did not become healthy on {base_url} ({last})")


def _sidebar_badge_count(base_url: str, session_id: str) -> int:
    """Return the session's ``pending_elicitations_count`` — the source the SPA
    sidebar's "Needs response" badge renders from (LIST endpoint)."""
    resp = httpx.get(f"{base_url}/v1/sessions?limit=200&visibility=all", timeout=10.0)
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("id") == session_id:
            return int(item.get("pending_elicitations_count") or 0)
    raise AssertionError(f"session {session_id} not present in list response")


def _pending_elicitation_id(base_url: str, session_id: str) -> str | None:
    """The correlation id of the session's first outstanding elicitation."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    events = resp.json().get("pending_elicitations") or []
    return events[0].get("elicitation_id") if events else None


@pytest.mark.timeout(240)
def test_subagent_needs_response_survives_restart_and_answer(tmp_path: Path) -> None:
    """A parked sub-agent elicitation must clear once the user answers, even
    after the sub-agent's runner died (exit 143) and Omnigent was restarted."""
    port = _find_free_port()
    db_path = tmp_path / "test.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml = tmp_path / "hello_world.yaml"
    agent_yaml.write_text(_TEST_AGENT_YAML)
    base_url = f"http://127.0.0.1:{port}"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # No real provider calls happen (the hook parks a server-side future); point
    # the harness at an unroutable base URL so nothing leaks to a real provider.
    server_env = {
        **os.environ,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    apply_server_env(server_env, _REPO_ROOT)
    runner_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
        "OPENAI_API_KEY": "mock-key",
    }

    server: subprocess.Popen | None = None
    runner: subprocess.Popen | None = None
    server_log = runner_log = None
    try:
        server_log = open(tmp_path / "server.log", "w")  # noqa: SIM115
        runner_log = open(tmp_path / "runner.log", "w")  # noqa: SIM115
        server = subprocess.Popen(
            _server_command(port, db_path, artifact_dir, agent_yaml),
            env=server_env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        runner = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )
        _wait_health(base_url, server, runner_id=runner_id)

        # 1. Create a runner-bound session (the sub-agent's session).
        bundle = _build_hello_world_bundle()
        create = httpx.post(
            f"{base_url}/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            timeout=30.0,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        patch = httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch.raise_for_status()
        assert _sidebar_badge_count(base_url, session_id) == 0

        # 2. Park a real permission-request elicitation via the claude-native
        #    hook. It long-polls (blocks), so drive it from a background thread.
        park_outcome: dict[str, str] = {}

        def _park() -> None:
            # The parked request dies when the server restarts; record the
            # outcome so a parking failure is diagnosable rather than opaque.
            try:
                resp = httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                    json={"tool_name": "Bash", "tool_input": {"command": "ls"}},
                    timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
                )
                park_outcome["result"] = f"HTTP {resp.status_code}: {resp.text[:120]}"
            except httpx.HTTPError as exc:
                park_outcome["result"] = f"{type(exc).__name__}: {exc}"

        park_thread = threading.Thread(target=_park, daemon=True)
        park_thread.start()

        # The badge shows "Needs response" once the prompt is parked.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _sidebar_badge_count(base_url, session_id) != 1:
            time.sleep(0.25)
        parked = park_outcome.get("result", "pending")
        assert _sidebar_badge_count(base_url, session_id) == 1, (
            f"elicitation did not park (park request outcome: {parked})"
        )
        elicitation_id = _pending_elicitation_id(base_url, session_id)
        assert elicitation_id is not None, "no parked elicitation id"

        # 3. The sub-agent's harness/message reader dies (SIGTERM = exit 143) and
        #    does NOT reconnect.
        terminate_process(runner)

        # 4. Restart Omnigent (recycle the server against the same durable store).
        #    The in-memory index is wiped; the persisted count survives. The dead
        #    runner never reconnects, so _on_runner_connect never reconciles it.
        terminate_process(server)
        park_thread.join(timeout=5)
        server = subprocess.Popen(
            _server_command(port, db_path, artifact_dir, agent_yaml),
            env=server_env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_health(base_url, server, runner_id=None)

        # The orphaned count survives the restart: the badge is still stuck at 1
        # before any answer, so clearing it below is the resolve's doing.
        assert _sidebar_badge_count(base_url, session_id) == 1, (
            "restart did not preserve the orphaned 'Needs response' badge"
        )

        # 5. The user answers the restarted session; the answer must clear the badge.
        resolve = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
            headers={"Content-Type": "application/json"},
            timeout=15.0,
        )
        assert resolve.status_code in (200, 202), (
            f"resolve rejected: {resolve.status_code} body={resolve.text[:200]}"
        )

        # Give the resolve a moment to propagate to the sidebar count source.
        final_count = _sidebar_badge_count(base_url, session_id)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and final_count != 0:
            time.sleep(0.5)
            final_count = _sidebar_badge_count(base_url, session_id)

        assert final_count == 0, (
            "after the sub-agent runner died (exit 143) and Omnigent restarted, "
            "the user's answer never registered — the session stays "
            f"'Needs response' (pending_elicitations_count={final_count})."
        )
    finally:
        terminate_process(runner)
        terminate_process(server)
        if server_log is not None:
            server_log.close()
        if runner_log is not None:
            runner_log.close()
