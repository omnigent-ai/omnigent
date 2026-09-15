"""End-to-end coverage for child-session survival of the parent runner's death.

Scenario: mid-turn child sessions die with the parent's runner — co-location has
no opt-out, and the existing stale-binding heal is reactive-only.

An orchestrator (running with the omnigent MCP tools) creates child sessions
via ``sys_session_create`` and drives them with ``sys_session_send``. A child
created with a ``parent_session_id`` is unconditionally *co-located* onto the
parent's runner. When the parent's runner process dies while a child turn is
in flight, the child dies with it and its in-flight work is lost, because:

  * co-location has **no opt-out** at create time, and
  * the stale-binding heal is **reactive-only** — nothing proactively re-binds a
    mid-turn child to a live runner owned by the same user and resumes it.

The report also states an explicit status invariant: *a session bound to a
dead runner must never report ``status: running``, and a tombstoned session
must never report ``running`` either.*

These tests drive the faithful user journey against a live server + runner(s)
(no shortcut into the internal co-location function): a real parent session
bound to a real runner, a real co-located child created through
``POST /v1/sessions`` with ``parent_session_id``, a real mid-turn turn held in
flight via the mock-LLM gate, and a real ``.kill()`` of the parent's runner
process. Two independent, fix-flippable assertions:

  * ``test_orphaned_mid_turn_child_recovers_on_live_runner`` — after the
    parent's runner dies and a **different-id live runner for the same user**
    comes online, the orphaned mid-turn child must recover (re-bound to a live
    runner or its held work resumed). On the buggy build it is never adopted:
    it stays pinned to the dead runner, settles ``failed``, and its work is
    lost.
  * ``test_tombstoned_child_on_dead_runner_not_running`` — after the child's
    runner is killed and the child is tombstoned (``sys_session_close`` →
    ``archived=True`` + closed labels), it must not report ``status: running``.
    On the buggy build the tombstoned, dead-runner-bound child keeps reporting
    ``running`` for the full ~10s disconnect grace window.

Both tests FAIL on the current (buggy) build and are the fail->pass targets for
the fix.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e.conftest import (
    _REPO_ROOT,
    configure_mock_llm,
    find_free_port,
    lookup_agent_id,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
    upload_agent,
)
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S
from tests.e2e.test_host_e2e import _write_smoke_agent_yaml

# Server anti-flap window before a disconnected runner's sessions are settled.
# Mirrors RUNNER_DISCONNECT_GRACE_S in omnigent/server/routes/_sessions.
GRACE_S = 10.0


def _base_env(config_home: Path, mock: str) -> dict:
    """Environment shared by the spawned server and runner processes."""
    return {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock}/v1",
        "OMNIGENT_CONFIG_HOME": str(config_home),
    }


def _spawn_server(
    port: int,
    db_path: Path,
    artifact_dir: Path,
    log_path: Path,
    config_home: Path,
    mock: str,
) -> tuple[subprocess.Popen, str]:
    """Start a server with no tunnel-token allowlist (accepts any token-bound runner)."""
    env = _base_env(config_home, mock)
    apply_server_env(env, _REPO_ROOT)
    cfg = log_path.parent / f"server-{port}.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {"base_url": f"{mock}/v1", "api_key": "mock-key"},
                }
            }
        )
    )
    args = [
        server_executable(),
        "-m",
        "omnigent.cli",
        "server",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
        "--config",
        str(cfg),
    ]
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            args, env=env, cwd=compat_server_cwd(), stdout=log_handle, stderr=subprocess.STDOUT
        )
    base = f"http://localhost:{port}"
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early: {log_path.read_text()[-2000:]}")
        try:
            if httpx.get(f"{base}/health", timeout=2, trust_env=False).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise RuntimeError("server did not become healthy")


def _spawn_runner(
    base: str,
    runner_id: str,
    token: str,
    log_path: Path,
    config_home: Path,
    mock: str,
) -> subprocess.Popen:
    """Start a runner subprocess bound to the server via the tunnel token."""
    env = _base_env(config_home, mock)
    apply_server_env(env, _REPO_ROOT)
    runner_env = apply_runner_env(
        {
            **env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base,
        }
    )
    with open(log_path, "w") as log_handle:
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=runner_env,
            cwd=compat_runner_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )


def _online(client: httpx.Client, runner_id: str) -> bool:
    """Return True when the server reports the runner as online."""
    resp = client.get(f"/v1/runners/{runner_id}/status")
    return resp.status_code == 200 and resp.json().get("online") is True


def _snap(client: httpx.Client, session_id: str) -> dict:
    """Return the current session record."""
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json()


def _items_text(client: httpx.Client, session_id: str) -> str:
    """Return the session's items serialized as text, for marker checks."""
    resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"})
    resp.raise_for_status()
    return json.dumps(resp.json()["data"])


def _create_child(client: httpx.Client, *, agent_id: str, parent_id: str) -> str:
    """Create a sub-agent child session co-located onto the parent's runner.

    Drives the real create path (``POST /v1/sessions`` with
    ``parent_session_id``) exactly as ``sys_session_create`` does; no opt-out
    field is passed because none exists.
    """
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "parent_session_id": parent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def _wait_online(client: httpx.Client, runner_id: str, timeout: float) -> bool:
    """Poll until the runner is online (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _online(client, runner_id):
            return True
        time.sleep(0.5)
    return _online(client, runner_id)


def _wait_gate_pending(mock: str, timeout: float) -> bool:
    """Poll the mock-LLM gate until a turn is held in flight (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gate = httpx.get(f"{mock}/gate/pending", timeout=5, trust_env=False)
        if gate.status_code == 200 and gate.json().get("pending"):
            return True
        time.sleep(0.5)
    return False


def _terminate(proc: subprocess.Popen | None) -> None:
    """Best-effort terminate a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.timeout(300)
def test_orphaned_mid_turn_child_recovers_on_live_runner(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A mid-turn child must not die permanently when the parent's runner dies.

    Journey: R1 online -> create parent bound to R1 -> create co-located child
    (inherits R1, no opt-out) -> drive a child turn and hold it in flight ->
    kill R1 (parent's runner dies mid-turn) -> bring a different-id live runner
    R2 online for the same user -> the orphaned mid-turn child must recover
    (re-bound to a live runner OR its held work resumed).

    Buggy build: the child is never adopted onto R2 — it stays pinned to the
    dead R1, settles ``failed``, and its in-flight work is lost.
    """
    token1 = secrets.token_urlsafe(32)
    id1 = token_bound_runner_id(token1)
    token2 = secrets.token_urlsafe(32)
    id2 = token_bound_runner_id(token2)

    db = tmp_path / "server.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()

    server = runner1 = runner2 = None
    client: httpx.Client | None = None
    try:
        server, base = _spawn_server(
            find_free_port(), db, artifacts, logs / "server.log", config_home, mock_llm_server_url
        )
        client = httpx.Client(base_url=base, timeout=30, trust_env=False)
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )

        # --- R1 (the orchestrator's runner) online -------------------------
        runner1 = _spawn_runner(
            base, id1, token1, logs / "runner1.log", config_home, mock_llm_server_url
        )
        assert _wait_online(client, id1, timeout=40), "R1 never came online"

        agent_name = upload_agent(client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(client, agent_name)

        parent_resp = client.post("/v1/sessions", json={"agent_id": agent_id})
        parent_resp.raise_for_status()
        parent = parent_resp.json()["id"]
        client.patch(f"/v1/sessions/{parent}", json={"runner_id": id1}).raise_for_status()

        # --- co-located child: inherits the parent's runner, no opt-out ----
        child = _create_child(client, agent_id=agent_id, parent_id=parent)
        child_runner = _snap(client, child).get("runner_id")
        assert child_runner == id1, (
            f"expected co-located child to inherit parent runner {id1}, got {child_runner}"
        )

        # --- drive a child turn and hold it in flight ----------------------
        reset_mock_llm(mock_llm_server_url)
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )
        # First response blocks (turn held in flight). If the child is ever
        # re-driven on a live runner, it consumes the second response.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "HOLD", "block": True}, {"text": "ADOPTED_AND_RESUMED"}],
        )
        send_user_message_to_session(client, session_id=child, content="Reply HOLD only.")
        assert _wait_gate_pending(mock_llm_server_url, timeout=40), (
            "child turn never reached the mock-LLM gate"
        )
        assert _snap(client, child).get("status") in (
            "running",
            "waiting",
        ), "child never reached a mid-turn status"

        # --- parent's runner dies mid-turn ---------------------------------
        runner1.kill()
        runner1.wait(timeout=10)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _online(client, id1):
            time.sleep(0.5)
        assert not _online(client, id1), "R1 still reported online after being killed"

        # --- a different-id live runner for the same user comes online -----
        runner2 = _spawn_runner(
            base, id2, token2, logs / "runner2.log", config_home, mock_llm_server_url
        )
        assert _wait_online(client, id2, timeout=40), "R2 never came online"

        # --- the orphaned mid-turn child must recover ----------------------
        recovered = False
        last = {}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            snap = _snap(client, child)
            bound = snap.get("runner_id")
            rebound = bound is not None and bound != id1 and _online(client, bound)
            resumed = "ADOPTED_AND_RESUMED" in _items_text(client, child)
            last = {
                "status": snap.get("status"),
                "runner_id": bound,
                "rebound": rebound,
                "resumed": resumed,
            }
            if rebound or resumed:
                recovered = True
                break
            time.sleep(3)

        assert recovered, (
            "orphaned mid-turn child was never adopted onto a live runner after the "
            f"parent's runner died: {last}. On the buggy build it stays pinned to the "
            f"dead runner {id1}, settles 'failed', and its in-flight work is lost. A live "
            f"runner {id2} owned by the same user was online and available for adoption."
        )
    finally:
        if client is not None:
            client.close()
        for proc in (server, runner1, runner2):
            _terminate(proc)


@pytest.mark.timeout(300)
def test_tombstoned_child_on_dead_runner_not_running(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A tombstoned, dead-runner-bound child must not report ``status: running``.

    Journey: R1 online -> create parent bound to R1 -> create co-located child
    -> drive a child turn and hold it in flight -> kill R1 (child's runner
    dies) -> tombstone the child (``sys_session_close`` semantics: archived +
    closed labels/title) -> the child must not keep reporting ``running``.

    Buggy build: the tombstoned child bound to the dead runner keeps reporting
    ``status: running`` for the full ~10s disconnect grace window, violating
    the report's explicit invariant.
    """
    token1 = secrets.token_urlsafe(32)
    id1 = token_bound_runner_id(token1)

    db = tmp_path / "server.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()

    server = runner1 = None
    client: httpx.Client | None = None
    try:
        server, base = _spawn_server(
            find_free_port(), db, artifacts, logs / "server.log", config_home, mock_llm_server_url
        )
        client = httpx.Client(base_url=base, timeout=30, trust_env=False)
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )

        runner1 = _spawn_runner(
            base, id1, token1, logs / "runner1.log", config_home, mock_llm_server_url
        )
        assert _wait_online(client, id1, timeout=40), "R1 never came online"

        agent_name = upload_agent(client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(client, agent_name)

        parent_resp = client.post("/v1/sessions", json={"agent_id": agent_id})
        parent_resp.raise_for_status()
        parent = parent_resp.json()["id"]
        client.patch(f"/v1/sessions/{parent}", json={"runner_id": id1}).raise_for_status()

        child = _create_child(client, agent_id=agent_id, parent_id=parent)
        assert _snap(client, child).get("runner_id") == id1, (
            "child did not inherit the parent's runner"
        )

        # Drive a child turn and hold it in flight.
        reset_mock_llm(mock_llm_server_url)
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )
        configure_mock_llm(mock_llm_server_url, [{"text": "HOLD", "block": True}])
        send_user_message_to_session(client, session_id=child, content="Reply HOLD only.")
        assert _wait_gate_pending(mock_llm_server_url, timeout=40), (
            "child turn never reached the mock-LLM gate"
        )
        for _ in range(30):
            if _snap(client, child).get("status") in ("running", "waiting"):
                break
            time.sleep(0.5)
        assert _snap(client, child).get("status") == "running", "child never reached 'running'"

        # The child's runner dies mid-turn.
        runner1.kill()
        runner1.wait(timeout=10)
        assert _snap(client, child).get("status") == "running", (
            "precondition: child should still show running at kill time"
        )

        # Tombstone the child (sys_session_close semantics).
        title = _snap(client, child).get("title") or "smoke:x"
        client.patch(
            f"/v1/sessions/{child}",
            json={
                "title": f"{title}:closed:{child}",
                "archived": True,
                "labels": {"omnigent.closed": "true"},
            },
        ).raise_for_status()

        # Invariant: a tombstoned session bound to a dead runner must not keep
        # reporting 'running'. Require it to leave 'running' well within the
        # grace window; the buggy build lies for the full grace window.
        settled = False
        last_status = None
        deadline = time.monotonic() + (GRACE_S - 4.0)
        while time.monotonic() < deadline:
            snap = _snap(client, child)
            last_status = snap.get("status")
            assert snap.get("archived") is True, "child was not tombstoned (archived)"
            if last_status != "running":
                settled = True
                break
            time.sleep(0.5)

        assert settled, (
            f"tombstoned child bound to a dead runner still reports status='running' "
            f"{GRACE_S - 4.0:.0f}s after being closed (last status={last_status!r}). "
            "The report requires that a tombstoned / dead-runner-bound session must "
            "never report 'running'; the buggy build lies for the full "
            f"{GRACE_S:.0f}s disconnect grace window."
        )
    finally:
        if client is not None:
            client.close()
        for proc in (server, runner1):
            _terminate(proc)
