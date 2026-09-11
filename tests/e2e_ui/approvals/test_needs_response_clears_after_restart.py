"""Multiagent "Needs response" stuck after a runner death and a server restart.

A sub-agent asks a question, the session sits
at "Needs response", the harness message reader dies (``exit code 143`` / SIGTERM),
the user restarts Omnigent, and the session stays "Needs response" while the answer
never reaches the agent.

This drives the deterministic, environment-independent core of that failure:

1. A runner-bound session (modelling the sub-agent's session, which runs on its
   own transient runner) parks a real ``permission-request`` elicitation via the
   claude-native hook. The persisted ``pending_elicitation_count`` becomes 1, so
   the sidebar renders the "Needs response" badge
   (``SessionStateBadge`` reads ``pending_elicitations_count`` via
   ``useSessionState``).
2. The runner is killed with SIGTERM (the reported ``exit code 143`` message-reader
   death) and is NOT restarted — a dispatched sub-agent's per-turn runner does not
   come back on its own.
3. Omnigent is restarted (the server process recycles against the same durable
   store). The in-memory pending-elicitation index dies with the process; the
   persisted count survives on the conversation row. ``_on_runner_connect``'s
   reconcile is keyed per-runner and never fires for the dead sub-agent runner, so
   the badge stays "Needs response".
4. The user answers via the resolve endpoint. Because the in-memory index is empty
   after the restart, ``pending_elicitations.resolve()`` early-returns without
   firing the count-persist hook, so the persisted count is never decremented — the
   badge stays stuck and the answer never registers.

The test asserts the behaviour a *fixed* build must satisfy: after the user
answers the restarted session, the pending-elicitation badge must clear
(``pending_elicitations_count == 0``). On the buggy build the answer is a no-op
and the count stays 1, so this assertion fails — the fail→pass target for the fix.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_server_env, server_executable
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
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    raise RuntimeError(f"server did not become healthy on {base_url} ({last})")


def _sidebar_badge_count(base_url: str, session_id: str) -> int:
    """Return the session's ``pending_elicitations_count`` — the source the SPA
    sidebar's "Needs response" badge renders from (LIST endpoint)."""
    resp = httpx.get(f"{base_url}/v1/sessions?limit=200", timeout=10.0)
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


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM a process, escalating to SIGKILL after a grace period."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


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

    server_log = open(tmp_path / "server.log", "w")  # noqa: SIM115
    runner_log = open(tmp_path / "runner.log", "w")  # noqa: SIM115
    server = subprocess.Popen(
        _server_command(port, db_path, artifact_dir, agent_yaml),
        env=server_env, stdout=server_log, stderr=subprocess.STDOUT,
    )
    runner = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=runner_env, stdout=runner_log, stderr=subprocess.STDOUT,
    )
    try:
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
            json={"runner_id": runner_id}, timeout=10.0,
        )
        patch.raise_for_status()
        assert _sidebar_badge_count(base_url, session_id) == 0

        # 2. Park a real permission-request elicitation via the claude-native
        #    hook. It long-polls (blocks), so drive it from a background thread.
        def _park() -> None:
            try:
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                    json={"tool_name": "Bash", "tool_input": {"command": "ls"}},
                    timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
                )
            except httpx.HTTPError:
                pass  # the parked request dies when the server restarts

        park_thread = threading.Thread(target=_park, daemon=True)
        park_thread.start()

        # The badge shows "Needs response" once the prompt is parked.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _sidebar_badge_count(base_url, session_id) != 1:
            time.sleep(0.25)
        assert _sidebar_badge_count(base_url, session_id) == 1, "elicitation did not park"
        elicitation_id = _pending_elicitation_id(base_url, session_id)
        assert elicitation_id is not None, "no parked elicitation id"

        # 3. The sub-agent's harness/message reader dies (SIGTERM = exit 143) and
        #    does NOT reconnect.
        _terminate(runner)

        # 4. Restart Omnigent (recycle the server against the same durable store).
        #    The in-memory index is wiped; the persisted count survives. The dead
        #    runner never reconnects, so _on_runner_connect never reconciles it.
        _terminate(server)
        server = subprocess.Popen(
            _server_command(port, db_path, artifact_dir, agent_yaml),
            env=server_env, stdout=server_log, stderr=subprocess.STDOUT,
        )
        _wait_health(base_url, server, runner_id=None)

        # 5. The user answers the restarted session (as they would from the chat
        #    or the Inbox). A correct build clears the badge; the buggy build's
        #    resolve() early-returns on the empty in-memory index and never
        #    decrements the persisted count.
        resolve = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
            headers={"Content-Type": "application/json"},
            timeout=15.0,
        )
        assert resolve.status_code in (200, 202), f"resolve rejected: {resolve.status_code}"

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
        _terminate(runner)
        _terminate(server)
        server_log.close()
        runner_log.close()
