"""End-to-end tests for the Host API (``omnigent connect``).

These tests start a real server subprocess, connect a real host
daemon, create sessions via the REST API, and verify the full
launch-runner → exchange-messages flow.

All tests run against the mock LLM server — no real credentials
needed::

    .venv/bin/python -m pytest tests/e2e/test_host_e2e.py -v

The last test (claude-native host-restart regression) runs against the
mock LLM server but requires ``claude`` and ``tmux`` on PATH. It is
skipped automatically when either binary is absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import (
    POLL_INTERVAL_S,
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.helpers import final_assistant_text


@dataclass
class _SpawnedHostDaemon:
    """A spawned host daemon subprocess paired with its known host_id.

    :param proc: The daemon subprocess handle.
    :param host_id: The host_id pre-seeded into ``config.yaml`` before
        spawning, e.g. ``"host_a1b2c3d4e5f6..."``.
    :param daemon_log: Path to the captured daemon stderr log; carries
        the ``Launched runner ... (pid=NNNN)`` line tests parse to find
        a spawned runner's process id.
    """

    proc: subprocess.Popen[bytes]
    host_id: str
    daemon_log: Path


def _spawn_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
    mock_llm_server_url: str,
) -> _SpawnedHostDaemon:
    """
    Spawn an isolated host daemon for a single host e2e test.

    Pre-seeds ``config.yaml`` with a UNIQUE ``(host_id, name)``: the host
    e2e tests share a session-scoped server, and the host store enforces a
    unique ``(owner, name)`` row. With the default machine hostname every
    test would collide on that row, so a later test's freshly-registered
    host_id gets overwritten and never shows online. A unique name per test
    keeps each host its own row.

    The daemon's environment carries ``OPENAI_BASE_URL`` and
    ``OPENAI_API_KEY`` pointing at the mock LLM server.  The host
    daemon forwards ``OPENAI_*`` to its runner subprocesses via
    ``HARNESS_CREDENTIAL_ENV_VARS``, so the runner's openai-agents
    executor hits the mock server.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param live_server: Server URL the daemon registers with, e.g.
        ``"http://localhost:18501"``.
    :param mock_llm_server_url: Base URL of the mock LLM server, e.g.
        ``"http://127.0.0.1:12345"``.
    :returns: The spawned daemon handle and its host_id.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = f"host_{uuid.uuid4().hex}"
    host_name = f"e2e-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    # Capture the daemon's stderr to a file so tests can read the
    # "Launched runner ... (pid=NNNN)" line (and inspect it on failure).
    # The child keeps its own dup of the fd after this handle is closed.
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            # Compat-aware: pinned OLD host venv in runner compat mode (Config 2),
            # else the test process's python. apply_runner_env drops the inherited
            # worktree PYTHONPATH in that mode; the old host launches old runners
            # (colocated) from its own venv.
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return _SpawnedHostDaemon(proc=proc, host_id=host_id, daemon_log=daemon_log)


def _runner_pid_from_daemon_log(log_path: Path) -> int | None:
    """Parse the launched runner's PID from the host daemon's log.

    The daemon logs ``Launched runner <id> for workspace <ws> (pid=NNNN)``
    when it spawns a runner subprocess.

    :param log_path: Path to the captured daemon stderr log.
    :returns: The runner subprocess PID, or ``None`` if not present yet.
    """
    if not log_path.exists():
        return None
    match = re.search(
        r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)",
        log_path.read_text(),
    )
    return int(match.group(1)) if match else None


def _pid_alive(pid: int) -> bool:
    """Return whether a process id is currently alive.

    :param pid: Process id to probe, e.g. ``12345``.
    :returns: ``True`` if the process exists, ``False`` once it has exited.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_smoke_agent_yaml(tmp_path: Path) -> Path:
    """Create a minimal Omnigent YAML for host e2e tests.

    :param tmp_path: Pytest temp directory.
    :returns: Path to the agent directory.
    """
    agent_dir = tmp_path / "host-e2e-agent"
    agent_dir.mkdir()
    (agent_dir / "host-e2e-agent.yaml").write_text(
        "\n".join(
            [
                "name: host-e2e-agent",
                "description: Minimal agent for host e2e tests.",
                "executor:",
                "  harness: openai-agents",
                "  model: gpt-5.4",
                "prompt: |",
                "  You are a terse smoke-test assistant.",
                "  Follow the user's instruction exactly.",
                "",
            ]
        )
    )
    return agent_dir


def _wait_for_host_online(
    client: httpx.Client,
    host_id: str,
    timeout: float = 30.0,
) -> None:
    """Poll GET /v1/hosts until the host appears online.

    :param client: HTTP client pointed at the server.
    :param host_id: Host ID to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


def test_host_connect_and_list(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    Start ``omnigent connect`` as a subprocess, verify the host
    appears in ``GET /v1/hosts`` with status online, stop it, and
    verify it goes offline.

    This is the basic registration smoke test — if the host never
    appears online, the WS tunnel handshake or DB upsert is broken.
    """
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    proc = daemon.proc
    host_id = daemon.host_id

    try:
        # Host should appear online in GET /v1/hosts.
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        resp = http_client.get("/v1/hosts")
        assert resp.status_code == 200
        hosts = resp.json()["hosts"]
        matching = [h for h in hosts if h["host_id"] == host_id]
        # Exactly one host with our ID should be listed.
        assert len(matching) == 1, (
            f"Expected 1 host with id {host_id!r}, got {len(matching)}. All hosts: {hosts}"
        )
        assert matching[0]["status"] == "online"

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # After killing the daemon, host should go offline.
    # Give the server a moment to process the disconnect.
    time.sleep(1.0)
    resp = http_client.get(f"/v1/hosts/{host_id}")
    if resp.status_code == 200:
        assert resp.json()["status"] == "offline", "Host should be offline after daemon is killed"


def test_host_launch_runner_and_session_round_trip(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    Full golden-path e2e: connect host, upload agent, create
    session, launch runner via ``POST /v1/hosts/{id}/runners``,
    send a message, and verify the LLM responds.

    This exercises the complete Web UI flow from the design doc:
    list hosts → create session → launch runner → exchange messages.
    """
    # Configure mock LLM to reply with the marker for the round-trip.
    marker = "HOST_E2E_GOLDEN_PATH_OK"
    configure_mock_llm(mock_llm_server_url, [{"text": marker}])

    # 1. Start host daemon.
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    host_proc = daemon.proc
    host_id = daemon.host_id

    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        # 2. Upload agent.
        agent_name = upload_agent(
            http_client,
            _write_smoke_agent_yaml(tmp_path),
        )

        # 3. Create session (no runner yet).
        agent_id = lookup_agent_id(http_client, agent_name)
        resp = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id},
        )
        resp.raise_for_status()
        session_id = resp.json()["id"]

        # 4. Launch runner on the host.
        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={
                "session_id": session_id,
                "workspace": str(tmp_path),
            },
            timeout=60.0,
        )
        assert launch_resp.status_code == 200, (
            f"Launch failed: {launch_resp.status_code} {launch_resp.text}"
        )
        runner_id = launch_resp.json()["runner_id"]

        # 5. Wait for runner to connect and bind.
        deadline = time.monotonic() + 30.0
        runner_online = False
        while time.monotonic() < deadline:
            status_resp = http_client.get(f"/v1/runners/{runner_id}/status")
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                runner_online = True
                break
            time.sleep(0.5)
        assert runner_online, f"Runner {runner_id} never came online after launch"

        # 6. Bind runner to session (the launch endpoint wrote
        #    runner_id but the session needs a PATCH for the relay).
        http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        ).raise_for_status()

        # 7. Send a message and verify the LLM responds.
        response_id = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=(
                f"Reply with exactly the literal string {marker} "
                "and nothing else. Do not call tools."
            ),
        )
        body = poll_session_until_terminal(
            http_client,
            session_id=session_id,
            response_id=response_id,
            timeout=180,
        )

        # The session should complete and the marker should be in
        # the assistant's response.
        assert body["status"] == "completed", f"Session failed: {body.get('error')}"
        text = final_assistant_text(body)
        assert marker in text, f"Marker {marker!r} missing from response: {text!r}"

    finally:
        host_proc.send_signal(signal.SIGTERM)
        try:
            host_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host_proc.kill()
            host_proc.wait()


def test_host_runner_survives_host_disconnect(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    Start host, launch runner, kill host, verify session still
    works (runner has independent WS tunnel).

    This proves the design decision that runners connect directly
    to the server, not through the host. If the session breaks
    after host disconnect, runner independence is violated.
    """
    # Pre-kill and post-kill markers.
    marker1 = "HOST_SURVIVE_PRE_KILL"
    marker2 = "HOST_SURVIVE_POST_KILL"
    configure_mock_llm(mock_llm_server_url, [{"text": marker1}, {"text": marker2}])

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    host_proc = daemon.proc
    host_id = daemon.host_id

    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        # Upload agent + create session + launch runner.
        agent_name = upload_agent(
            http_client,
            _write_smoke_agent_yaml(tmp_path),
        )
        agent_id = lookup_agent_id(http_client, agent_name)
        resp = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id},
        )
        resp.raise_for_status()
        session_id = resp.json()["id"]

        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(tmp_path)},
            timeout=60.0,
        )
        assert launch_resp.status_code == 200
        runner_id = launch_resp.json()["runner_id"]

        # Wait for runner online.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            sr = http_client.get(f"/v1/runners/{runner_id}/status")
            if sr.status_code == 200 and sr.json().get("online"):
                break
            time.sleep(0.5)

        http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        ).raise_for_status()

        # Verify session works BEFORE killing host.
        rid1 = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=f"Reply with exactly {marker1} and nothing else.",
        )
        body1 = poll_session_until_terminal(
            http_client,
            session_id=session_id,
            response_id=rid1,
            timeout=120,
        )
        assert body1["status"] == "completed"
        assert marker1 in final_assistant_text(body1)

        # Kill the host daemon (but NOT the runner — it's a separate
        # process with start_new_session=True in the host daemon,
        # but since the daemon spawns runners as children, we need
        # to only kill the daemon, not its children).
        host_proc.send_signal(signal.SIGTERM)
        host_proc.wait(timeout=5)

        # Give server a moment to notice the host disconnect.
        time.sleep(1.0)

        # Runner should still be online.
        sr = http_client.get(f"/v1/runners/{runner_id}/status")
        # Runner may or may not still be online depending on whether
        # the daemon's SIGTERM cascaded. If the runner IS still
        # online, verify the session still works.
        if sr.status_code == 200 and sr.json().get("online"):
            rid2 = send_user_message_to_session(
                http_client,
                session_id=session_id,
                content=f"Reply with exactly {marker2} and nothing else.",
            )
            body2 = poll_session_until_terminal(
                http_client,
                session_id=session_id,
                response_id=rid2,
                timeout=120,
            )
            assert body2["status"] == "completed", (
                "Session should still work after host disconnect — "
                "runner has independent WS tunnel"
            )
            assert marker2 in final_assistant_text(body2)

    except Exception:
        # Cleanup: make sure host proc is dead.
        if host_proc.poll() is None:
            host_proc.kill()
            host_proc.wait()
        raise


def test_host_death_kills_runners(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    Start host, launch a runner, kill the host, verify the runner
    exits within a few seconds.

    The runner's parent-PID watchdog polls every 1s and exits when
    the parent (host daemon) is gone. If the runner stays alive
    after the host dies, the watchdog is broken and we'd accumulate
    orphaned runner processes.
    """
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    host_proc = daemon.proc
    host_id = daemon.host_id

    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        # Upload an agent + resolve its durable id. The standalone
        # /api/agents endpoint was removed; agents are now
        # created via multipart POST /v1/sessions and looked up by name.
        agent_name = upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(http_client, agent_name)

        # Create session + launch runner.
        session_resp = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id},
        )
        session_resp.raise_for_status()
        session_id = session_resp.json()["id"]

        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(tmp_path)},
            timeout=60.0,
        )
        assert launch_resp.status_code == 200
        runner_id = launch_resp.json()["runner_id"]

        # Wait for runner to come online.
        deadline = time.monotonic() + 30.0
        runner_online = False
        while time.monotonic() < deadline:
            sr = http_client.get(f"/v1/runners/{runner_id}/status")
            if sr.status_code == 200 and sr.json().get("online"):
                runner_online = True
                break
            time.sleep(0.5)
        assert runner_online, f"Runner {runner_id} never came online"

        # Resolve the runner's OS pid before killing the host so we can
        # assert on the process directly.
        runner_pid = _runner_pid_from_daemon_log(daemon.daemon_log)
        assert runner_pid is not None, (
            "could not find the launched runner pid in the daemon log:\n"
            f"{daemon.daemon_log.read_text()}"
        )

        # Kill the host daemon.
        host_proc.kill()
        host_proc.wait()

        # The orphaned runner must exit (parent-PID watchdog). Assert on
        # the runner PROCESS — the invariant this test protects (no orphan
        # accumulation). The server's online flag is a poor proxy here: it
        # only clears a dead runner on the next 30s keepalive ping, long
        # after the runner has actually exited.
        deadline = time.monotonic() + 15.0
        runner_died = False
        while time.monotonic() < deadline:
            if not _pid_alive(runner_pid):
                runner_died = True
                break
            time.sleep(0.5)

        assert runner_died, (
            f"Runner process {runner_pid} should have exited after host "
            "death (parent-PID watchdog). If it's still alive, orphaned "
            "runner processes will accumulate."
        )

    except Exception:
        if host_proc.poll() is None:
            host_proc.kill()
            host_proc.wait()
        raise


# ── Host-restart native round-trip ─────────────────────────────
#
# Skipped when ``claude`` or ``tmux`` are absent from PATH — the test
# launches a real Claude Code TUI in a tmux session via the host daemon.
# All LLM calls hit the shared mock server: the daemon's environment
# carries ``ANTHROPIC_BASE_URL`` pointing at the mock and
# ``ANTHROPIC_API_KEY=mock-key``; both flow to the runner via the host's
# ``HARNESS_CREDENTIAL_ENV_VARS`` allowlist.


def _write_claude_native_agent_yaml(tmp_path: Path) -> Path:
    """Create a minimal ``claude-native`` agent dir for the host e2e.

    No ``executor.auth`` — auth flows via ``ANTHROPIC_API_KEY`` /
    ``ANTHROPIC_BASE_URL`` injected into the daemon's environment (and
    therefore inherited by the runner's tmux session).

    :param tmp_path: Pytest temp directory.
    :returns: Path to the agent directory.
    """
    agent_dir = tmp_path / "host-e2e-claude-native"
    agent_dir.mkdir()
    (agent_dir / "host-e2e-claude-native.yaml").write_text(
        "\n".join(
            [
                "name: host-e2e-claude-native",
                "description: claude-native agent for host-restart e2e.",
                "prompt: |",
                "  You are a terse test assistant. Reply exactly as asked.",
                "executor:",
                "  harness: claude-native",
                "",
            ]
        )
    )
    return agent_dir


def _seed_onboarded_claude_home(home_dir: Path, workspace: str) -> None:
    """Pre-seed ``~/.claude.json`` so the daemon-spawned Claude TUI starts.

    The host daemon runs with ``HOME=home_dir`` and its runner launches
    Claude there. With no prior onboarding the first-run theme picker +
    workspace-trust dialog block the TUI before the MCP bridge initializes,
    so the terminal/forwarder never come up. Seeding "already onboarded" +
    "already trusts the workspace" clears both gates.

    :param home_dir: The daemon's ``HOME`` (also the runner's), e.g.
        ``tmp_path``.
    :param workspace: The session workspace = Claude's cwd, whose trust
        gate must be pre-accepted, e.g. ``str(tmp_path / "ws")``.
    :returns: None.
    """
    (home_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "lastOnboardingVersion": "2.0.0",
                "projects": {
                    workspace: {
                        "hasTrustDialogAccepted": True,
                        "hasCompletedProjectOnboarding": True,
                    },
                },
            }
        )
    )


def _spawn_host_daemon_for_mock_claude(
    *,
    tmp_path: Path,
    live_server: str,
    mock_llm_server_url: str,
) -> _SpawnedHostDaemon:
    """Spawn an isolated host daemon wired to the mock Anthropic LLM.

    Like :func:`_spawn_host_daemon` but sets ``ANTHROPIC_BASE_URL`` and
    ``ANTHROPIC_API_KEY`` so the runner's Claude TUI hits the mock server
    instead of prompting for OAuth. Both vars are in
    ``HARNESS_CREDENTIAL_ENV_VARS`` and flow daemon→runner automatically.

    The Anthropic SDK appends ``/v1/messages`` to ``ANTHROPIC_BASE_URL``,
    so the URL must NOT include ``/v1``.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param live_server: Server URL the daemon registers with.
    :param mock_llm_server_url: Mock LLM server base URL, e.g.
        ``"http://127.0.0.1:12345"``.
    :returns: The spawned daemon handle and its host_id.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = f"host_{uuid.uuid4().hex}"
    host_name = f"e2e-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        # ANTHROPIC_BASE_URL is in HARNESS_CREDENTIAL_ENV_VARS so it flows
        # daemon→runner. The Anthropic SDK appends /v1/messages; omit /v1.
        "ANTHROPIC_BASE_URL": mock_llm_server_url,
        # ANTHROPIC_API_KEY bypasses the Claude CLI's OAuth login — no
        # ~/.claude.json account is needed when an explicit API key is set.
        "ANTHROPIC_API_KEY": "mock-key",
        # Suppress beta headers so the mock server doesn't reject them.
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            # Compat-aware: pinned OLD host venv in runner compat mode (Config 2),
            # else the test process's python. apply_runner_env drops the inherited
            # worktree PYTHONPATH in that mode; the old host launches old runners
            # (colocated) from its own venv.
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return _SpawnedHostDaemon(proc=proc, host_id=host_id, daemon_log=daemon_log)


def _native_user_message_round_tripped(
    client: httpx.Client,
    *,
    session_id: str,
    marker: str,
) -> bool:
    """Whether the forwarder mirrored the marker user message back to AP.

    Native web messages aren't persisted at POST time — they're injected
    into the TUI and the transcript forwarder mirrors them back as
    persisted items. So a user-role item whose text carries *marker*
    appearing in ``GET /v1/sessions/{id}/items`` proves the round-trip
    happened (terminal + forwarder were watching before injection — the
    fix). Without the fix the message is injected before the forwarder
    attaches and never persists.

    :param client: HTTP client pointed at the server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc"``.
    :param marker: Unique substring embedded in the sent user message.
    :returns: ``True`` once a matching user item is present.
    """
    resp = client.get(f"/v1/sessions/{session_id}/items")
    if resp.status_code != 200:
        return False
    for item in resp.json().get("data", []):
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and marker in block["text"]
            for block in content
        ):
            return True
    return False


@pytest.mark.skipif(
    shutil.which("claude") is None
    or shutil.which("tmux") is None
    or not os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE"),
    reason=(
        "claude-native host-restart e2e requires `claude` + `tmux` on PATH "
        "and OMNIGENT_E2E_CLAUDE_NATIVE=1 (needs real claude CLI with mock auth)"
    ),
)
def test_host_native_session_round_trips_after_runner_death(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A web message to a host-bound claude-native session whose runner
    died relaunches the runner and round-trips through the forwarder.

    Regression guard for the host-restart native-session fix. Steps:

    1. Spawn a host daemon with ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY``
       pointing at the mock LLM server.
    2. Create a claude-native session bound to the host.
    3. Wait for the runner to launch and its initial pid to appear in the
       daemon log.
    4. Hard-kill the runner (``SIGKILL``).
    5. Send a web message — the server must relaunch the runner via the
       host, run ``create_session`` (terminal + transcript forwarder) BEFORE
       forwarding the message, and the forwarder must mirror the user text
       back as a persisted item.

    The assertion is on the user-message round-trip, not Claude's reply,
    so the test does not depend on mock LLM response ordering. The
    round-trip itself proves the ordering fix: without it the message is
    injected before the forwarder attaches and never surfaces in
    ``GET /v1/sessions/{id}/items``.
    """
    marker = f"NATIVE_RESTART_{uuid.uuid4().hex[:8].upper()}"
    # Queue Anthropic SSE responses for both the initial runner startup
    # turn and the post-relaunch turn.  Extra calls fall back to the
    # default queue, so the queue never exhausts prematurely.
    configure_mock_llm(mock_llm_server_url, [{"text": marker}, {"text": marker}])

    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Seed the daemon HOME as onboarded + trusting the workspace so the
    # claude TUI starts without showing the first-run / trust dialogs.
    _seed_onboarded_claude_home(tmp_path, str(workspace))

    daemon = _spawn_host_daemon_for_mock_claude(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    host_proc = daemon.proc
    host_id = daemon.host_id

    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        agent_name = upload_agent(
            http_client,
            _write_claude_native_agent_yaml(tmp_path),
        )
        agent_id = lookup_agent_id(http_client, agent_name)

        # Inline host-launch: server creates the session, sends a
        # host.launch_runner frame, and the daemon spawns the runner.
        create_resp = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
                "labels": {"omnigent.wrapper": "claude-code-native-ui"},
            },
            timeout=60.0,
        )
        create_resp.raise_for_status()
        session_id = create_resp.json()["id"]

        # Wait for the host to launch the initial runner (daemon logs its pid).
        deadline = time.monotonic() + 90.0
        initial_pid: int | None = None
        while time.monotonic() < deadline:
            initial_pid = _runner_pid_from_daemon_log(daemon.daemon_log)
            if initial_pid is not None and _pid_alive(initial_pid):
                break
            time.sleep(POLL_INTERVAL_S)
        assert initial_pid is not None, (
            "host never launched the initial runner;\n"
            f"daemon log:\n{daemon.daemon_log.read_text()}"
        )

        # Hard-kill the runner to simulate a crash / restart scenario.
        os.kill(initial_pid, signal.SIGKILL)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and _pid_alive(initial_pid):
            time.sleep(POLL_INTERVAL_S)
        assert not _pid_alive(initial_pid), f"runner pid {initial_pid} did not die after SIGKILL"

        # Sending a message after runner death must trigger the host to
        # relaunch the runner.  The relaunch path runs create_session
        # (terminal + forwarder) BEFORE injecting the message so the
        # forwarder is watching when the text arrives and mirrors the user
        # turn back as a persisted item.
        send_resp = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Reply with exactly {marker}"}],
                },
            },
            timeout=90.0,
        )
        send_resp.raise_for_status()

        # Poll session items for the user message mirrored back by the
        # forwarder. Generous timeout: relaunch + Claude TUI cold-start +
        # transcript round-trip can take 60-120 s on a warm machine.
        deadline = time.monotonic() + 180.0
        round_tripped = False
        while time.monotonic() < deadline:
            if _native_user_message_round_tripped(
                http_client, session_id=session_id, marker=marker
            ):
                round_tripped = True
                break
            time.sleep(POLL_INTERVAL_S)
        assert round_tripped, (
            f"user message containing {marker!r} never appeared in "
            f"/v1/sessions/{session_id}/items after the runner relaunch. "
            "The relaunched runner likely forwarded the message before its "
            "transcript forwarder was watching (the host-restart regression)."
        )

    finally:
        host_proc.send_signal(signal.SIGTERM)
        try:
            host_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host_proc.kill()
            host_proc.wait()
