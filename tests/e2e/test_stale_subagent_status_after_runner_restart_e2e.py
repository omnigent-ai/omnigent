"""Check the supervisor's child status after a runner restart within grace.

A mock LLM holds the child mid-turn while the test kills its runner. A
replacement reconnects under the same ID before offline marking. After the
grace period, the supervisor's real session-list result must not report the
incomplete child as running or waiting.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S
from tests._helpers.compat import apply_runner_env, apply_server_env
from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    lookup_agent_id,
    register_inline_agent,
    reset_mock_llm,
)

_DISPATCH_ACK = "DISPATCH_ACK_STALE_STATUS_5823"
_STATUS_DONE = "STATUS_CHECKED_STALE_STATUS_5823"
_CHILD_COMPLETION = "RESEARCH_COMPLETE_STALE_STATUS_5823"
_STATUS_CALL_ID = "call_status_5823"

# Wait past the server's disconnect grace after the replacement is online.
_SETTLE_AFTER_RECONNECT_S = RUNNER_DISCONNECT_GRACE_S + 5.0

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_TIMEOUT_S = 90.0
_LOOPBACK_NO_PROXY = "localhost,127.0.0.1"

pytestmark = [pytest.mark.timeout(600, method="signal")]


def _ambient_free_environ() -> dict[str, str]:
    """Strip inherited runner identity so subprocesses bind to this stack."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        and k not in ("RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN")
    }


def _merged_no_proxy(env: dict[str, str]) -> str:
    """Include loopback hosts in the subprocess's NO_PROXY."""
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    parts = [p for p in existing.split(",") if p]
    for host in _LOOPBACK_NO_PROXY.split(","):
        if host not in parts:
            parts.append(host)
    return ",".join(parts)


class _RunnerRestartStack:
    """Own a server and a replaceable runner with a stable binding token."""

    def __init__(self, mock_llm_server_url: str, tmp_path: Path) -> None:
        self._mock_base = f"{mock_llm_server_url}/v1"
        self._tmp_path = tmp_path
        self._port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self._port}"
        self._db_path = tmp_path / "stale_status.db"
        self._artifact_dir = tmp_path / "artifacts"
        self._artifact_dir.mkdir()
        self.server_log = tmp_path / "server.log"
        self.runner_log = tmp_path / "runner.log"
        self._binding_token = uuid.uuid4().hex + uuid.uuid4().hex
        self.runner_id = token_bound_runner_id(self._binding_token)
        self._server_proc: subprocess.Popen[bytes] | None = None
        self._server_log_handle = None
        self._runner_proc: subprocess.Popen[bytes] | None = None
        self._runner_log_handle = None
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0, trust_env=False)

    def _server_env(self) -> dict[str, str]:
        env = {
            **_ambient_free_environ(),
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": self._mock_base,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": self._binding_token,
        }
        env["NO_PROXY"] = _merged_no_proxy(env)
        env["no_proxy"] = env["NO_PROXY"]
        apply_server_env(env, _REPO_ROOT)
        return env

    def _spawn_server(self) -> None:
        self._server_log_handle = open(self.server_log, "a")  # noqa: SIM115 — lives for the Popen lifetime; closed in teardown
        self._server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--port",
                str(self._port),
                "--database-uri",
                f"sqlite:///{self._db_path}",
                "--artifact-location",
                str(self._artifact_dir),
            ],
            env=self._server_env(),
            stdout=self._server_log_handle,
            stderr=subprocess.STDOUT,
        )

    def _spawn_runner(self) -> None:
        # Keep the runner on this worktree's PYTHONPATH.
        env = apply_runner_env(
            {
                **self._server_env(),
                "OMNIGENT_RUNNER_ID": self.runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": self._binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": self.base_url,
            }
        )
        env["NO_PROXY"] = _merged_no_proxy(env)
        env["no_proxy"] = env["NO_PROXY"]
        self._runner_log_handle = open(self.runner_log, "a")  # noqa: SIM115 — lives for the Popen lifetime; closed in teardown
        self._runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=self._runner_log_handle,
            stderr=subprocess.STDOUT,
        )

    def runner_online(self) -> bool:
        """Check the server's runner-online flag."""
        try:
            resp = httpx.get(
                f"{self.base_url}/v1/runners/{self.runner_id}/status",
                timeout=2,
                trust_env=False,
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.json().get("online") is True

    def wait_healthy(self, timeout: float = _HEALTH_TIMEOUT_S) -> None:
        """Wait until both the server and runner are ready."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{self.base_url}/health", timeout=2, trust_env=False)
                if health.status_code == 200 and self.runner_online():
                    return
            except httpx.HTTPError:
                # The stack may still be booting.
                pass
            time.sleep(0.25)
        server_tail = self.server_log.read_text()[-3000:] if self.server_log.exists() else ""
        runner_tail = self.runner_log.read_text()[-3000:] if self.runner_log.exists() else ""
        raise RuntimeError(
            f"Server/runner not healthy within {timeout}s.\n"
            f"Server log tail:\n{server_tail}\n"
            f"Runner log tail:\n{runner_tail}"
        )

    def start(self) -> None:
        """Spawn the server and the runner and wait for both to be ready."""
        self._spawn_server()
        self._spawn_runner()
        self.wait_healthy()

    def kill_runner(self) -> None:
        """SIGKILL the runner process — the mid-turn interruption."""
        assert self._runner_proc is not None
        self._runner_proc.kill()
        self._runner_proc.wait(timeout=10)
        if self._runner_log_handle is not None:
            self._runner_log_handle.close()
            self._runner_log_handle = None

    def spawn_replacement_runner(self, timeout: float = _HEALTH_TIMEOUT_S) -> None:
        """Restart the runner under the same ID and wait for reconnect."""
        self._spawn_runner()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.runner_online():
                return
            time.sleep(0.25)
        runner_tail = self.runner_log.read_text()[-3000:] if self.runner_log.exists() else ""
        raise RuntimeError(
            f"Replacement runner not online within {timeout}s.\nRunner log tail:\n{runner_tail}"
        )

    def teardown(self) -> None:
        """Kill both subprocesses and close file handles."""
        for proc in (self._runner_proc, self._server_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        for handle in (self._runner_log_handle, self._server_log_handle):
            if handle is not None:
                handle.close()
        self.client.close()


@pytest.fixture
def runner_restart_stack(
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[_RunnerRestartStack]:
    """Provide an isolated server and runner wired to the mock LLM."""
    stack = _RunnerRestartStack(mock_llm_server_url, tmp_path)
    stack.start()
    try:
        yield stack
    finally:
        stack.teardown()


def _create_session(client: httpx.Client, agent_name: str, runner_id: str) -> str:
    """Create a session for the agent on this runner."""
    agent_id = lookup_agent_id(client, agent_name)
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["id"])
    resp = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
    resp.raise_for_status()
    return session_id


def _post_user_message(client: httpx.Client, session_id: str, text: str) -> None:
    """Post a user message to the session."""
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    resp.raise_for_status()


def _session_blob(client: httpx.Client, session_id: str) -> str:
    """Return the session's items as JSON, or empty on a transient error."""
    try:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
    except httpx.HTTPError:
        return ""
    return json.dumps(resp.json().get("items", []))


def _session_status(client: httpx.Client, session_id: str) -> str:
    """Read status via the lightweight snapshot used by session-get-info."""
    try:
        resp = client.get(
            f"/v1/sessions/{session_id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return ""
    status = resp.json().get("status")
    return status if isinstance(status, str) else ""


def _find_child_session_id(client: httpx.Client, parent_session_id: str) -> str | None:
    """Find the supervisor's child session, if created."""
    resp = client.get(
        "/v1/sessions", params={"visibility": "all", "kind": "sub_agent", "limit": 50}
    )
    resp.raise_for_status()
    for row in resp.json().get("data", []):
        if not isinstance(row, dict):
            continue
        child_id = row.get("id")
        if not isinstance(child_id, str):
            continue
        snap = client.get(
            f"/v1/sessions/{child_id}",
            params={"include_items": "false", "include_liveness": "false"},
        )
        if snap.status_code != 200:
            continue
        if snap.json().get("parent_session_id") == parent_session_id:
            return child_id
    return None


def _poll_until(condition: Callable[[], bool], timeout: float, what: str) -> None:
    """Wait for a condition or fail with its description."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.25)
    raise AssertionError(f"Timed out after {timeout}s waiting for: {what}")


def _gate_pending(mock_url: str) -> bool:
    """Check whether a mock-LLM call is blocked on its gate."""
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _release_gate(mock_url: str) -> None:
    """Release the pending mock-LLM call."""
    resp = httpx.post(f"{mock_url}/gate/release", timeout=5, trust_env=False)
    resp.raise_for_status()


def _statuses_in_payload(payload: object, session_id: str) -> list[str]:
    """Collect statuses from nested tool-result rows referencing a session."""
    found: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            has_id = any(v == session_id for v in node.values() if isinstance(v, str))
            status = node.get("status")
            if has_id and isinstance(status, str):
                found.append(status)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)
    return found


def _status_tool_outputs(mock_url: str, parent_key: str, call_id: str) -> list[str]:
    """Read status-check outputs captured in the supervisor's LLM requests."""
    resp = httpx.get(
        f"{mock_url}/mock/requests", params={"key": parent_key}, timeout=5, trust_env=False
    )
    resp.raise_for_status()
    outputs: list[str] = []
    for request in resp.json().get("requests", []):
        if not isinstance(request, dict):
            continue
        input_items = request.get("input")
        if not isinstance(input_items, list):
            continue
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call_output":
                continue
            if item.get("call_id") != call_id:
                continue
            output = item.get("output")
            if isinstance(output, str):
                outputs.append(output)
    return outputs


def _decode_tool_output(output: str) -> object | None:
    """Accept JSON or a harness-repr'd Python dict in tool output."""
    try:
        return json.loads(output)
    except ValueError:
        pass
    try:
        return ast.literal_eval(output)
    except (ValueError, SyntaxError):
        return None


def _supervisor_reported_statuses(outputs: list[str], child_id: str) -> list[str]:
    """Extract child statuses from captured tool outputs."""
    statuses: list[str] = []
    for output in outputs:
        payload = _decode_tool_output(output)
        if payload is None:
            continue
        statuses.extend(_statuses_in_payload(payload, child_id))
    return statuses


def test_stopped_subagent_not_reported_running_after_runner_restart(
    runner_restart_stack: _RunnerRestartStack,
    mock_llm_server_url: str,
) -> None:
    """An interrupted child turn must not read as running after reconnect."""
    stack = runner_restart_stack
    client = stack.client
    reset_mock_llm(mock_llm_server_url)

    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-ss-parent-{uid}"
    child_model = f"mock-ss-child-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"

    parent_name = register_inline_agent(
        client,
        name=f"ss-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are the stale-status E2E test fixture supervisor. Dispatch "
            "the researcher sub-agent via sys_session_send when asked, and "
            "inspect sub-agent state with sys_session_list when asked for a "
            "status update."
        ),
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "researcher": {
                    "type": "agent",
                    "description": "Test-fixture researcher. Returns a marker.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": "You are the test-fixture researcher. Return the marker.",
                },
            },
        },
    )

    # The parent dispatches, then calls sys_session_list on the status bump.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_5823",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "researcher",
                                "title": "stale-status",
                                "args": "Fetch the marker.",
                            }
                        ),
                    }
                ],
            },
            {"text": f"{_DISPATCH_ACK}: researcher dispatched, waiting for its result."},
            {
                "tool_calls": [
                    {
                        "call_id": _STATUS_CALL_ID,
                        "name": "sys_session_list",
                        "arguments": "{}",
                    }
                ],
            },
            {"text": f"{_STATUS_DONE}: reported sub-agent state to the user."},
        ],
        key=parent_model,
    )
    # Keep the child mid-turn until its runner has been killed.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Research complete. {_CHILD_COMPLETION}", "block": True}],
        key=child_model,
    )

    session_id = _create_session(client, parent_name, stack.runner_id)
    _post_user_message(client, session_id, "Dispatch the researcher sub-agent.")

    # Wait for the parent's turn to end and the child's call to block.
    _poll_until(
        lambda: _DISPATCH_ACK in _session_blob(client, session_id),
        timeout=120,
        what="supervisor dispatch turn to complete (ack text in session items)",
    )
    _poll_until(
        lambda: _gate_pending(mock_llm_server_url),
        timeout=60,
        what="the researcher child to reach its gated LLM call",
    )

    child_id: str | None = None

    def _child_found() -> bool:
        nonlocal child_id
        child_id = _find_child_session_id(client, session_id)
        return child_id is not None

    _poll_until(_child_found, timeout=30, what="the researcher child session to exist")
    assert child_id is not None
    assert _session_status(client, child_id) == "running", (
        "Precondition: the gated child must be mid-turn (status 'running') before the "
        "interruption."
    )

    # Reconnect within grace; the old runner's child turn cannot resume.
    interrupted_at = time.monotonic()
    stack.kill_runner()
    _poll_until(
        lambda: not stack.runner_online(),
        timeout=5,
        what="the old runner tunnel to go offline",
    )
    stack.spawn_replacement_runner()
    reconnect_seconds = time.monotonic() - interrupted_at
    assert reconnect_seconds < RUNNER_DISCONNECT_GRACE_S, (
        f"Replacement connected after {reconnect_seconds:.1f}s, outside the "
        f"{RUNNER_DISCONNECT_GRACE_S:.1f}s disconnect grace"
    )
    _release_gate(mock_llm_server_url)

    # Inspect only after the disconnect grace has elapsed.
    time.sleep(_SETTLE_AFTER_RECONNECT_S)

    # The child did not produce its gated completion.
    child_completed = _CHILD_COMPLETION in _session_blob(client, child_id)
    assert not child_completed, "Precondition: the killed runner's child turn must not complete"

    # Ask the parent to inspect its child with sys_session_list.
    _post_user_message(
        client, session_id, "Status update: is the researcher sub-agent still running?"
    )
    _poll_until(
        lambda: _STATUS_DONE in _session_blob(client, session_id),
        timeout=120,
        what="the supervisor's status-check turn to complete",
    )

    raw_outputs = _status_tool_outputs(mock_llm_server_url, parent_model, _STATUS_CALL_ID)
    reported = _supervisor_reported_statuses(raw_outputs, child_id)
    rest_status = _session_status(client, child_id)

    assert reported, (
        f"The supervisor's status check reported no status at all for the stopped child "
        f"{child_id}: sys_session_list's sub_agents rows carry no status field and the "
        f"global sessions list omits sub-agent sessions, so the supervisor has no way to "
        f"learn the child's turn died with the interrupted runner (child completion "
        f"marker present: {child_completed}; child REST snapshot status: {rest_status!r}). "
        f"Raw status tool outputs: {raw_outputs[:3]!r}"
    )
    assert not (set(reported) & {"running", "waiting"}), (
        f"Stale sub-agent status after a runner interruption: the supervisor was told the "
        f"researcher child ({child_id}) is {reported!r}, but the child's turn died with the "
        f"killed runner process and can never complete (completion marker present in child "
        f"transcript: {child_completed}). REST snapshot status agrees: {rest_status!r}. "
        f"Because the replacement runner reconnected within the disconnect grace, the server "
        f"skipped offline-marking, and nothing afterwards reconciles a mid-turn session whose "
        f"turn evaporated — so the supervisor keeps believing a stopped sub-agent is still "
        f"running."
    )
