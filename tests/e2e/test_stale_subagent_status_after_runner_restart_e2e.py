"""End-to-end test: a stopped sub-agent must not read as still running.

A Polly-style supervisor dispatches a sub-agent and goes idle while the
child works. If the runner process is interrupted mid-child-turn (laptop
close, wifi drop, host daemon restart) and a replacement runner reconnects
promptly — inside the server's disconnect grace — the server skips
offline-marking for the runner's sessions. The child's in-flight turn died
with the old runner process, but the server never records that: no
``failed``/interrupted status is persisted for the child, no completion or
wake ever reaches the supervisor, and the supervisor's state-inspection
tool gives it nothing to reconcile with — ``sys_session_list`` sub-agent
rows carry no status field at all, and the global sessions list omits
sub-agent sessions entirely.

So after the interruption the supervisor's status check reports nothing
about the stopped child. Left with only its own "dispatched, waiting"
context, the supervisor insists the stopped sub-agent is still working —
the user has to manually chat with the sub-agent to re-kick it.

This test reproduces that journey deterministically:

1. The supervisor dispatches a gated researcher sub-agent (its mock-LLM
   response blocks until the test releases a gate), then ends its turn —
   the child is now mid-turn, status ``running``.
2. The test SIGKILLs the runner process (the interruption) and immediately
   spawns a replacement with the same binding token, so it reconnects as
   the same runner id within the disconnect grace. The child's turn is
   gone; the child will never complete.
3. After the grace has elapsed and the state has settled, the user bumps
   the supervisor to check on the sub-agent; the supervisor inspects via
   ``sys_session_list``. The test asserts the supervisor IS told the dead
   child's status, and that the status is not ``running``/``waiting``.

On a buggy build the first assertion fails: the captured tool output
mentions the child only as ``{agent, title, conversation_id}`` — no
status — so the supervisor cannot learn the child stopped; the failure
message includes the ground truth (the child never produced a completion).
A fix that surfaces the child's true state to the supervisor makes both
assertions pass; a fix that surfaces a stale ``running`` is caught by the
second.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke
with::

    pytest tests/e2e/test_stale_subagent_status_after_runner_restart_e2e.py -v --timeout=600
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

# Server-side reconnect grace (RUNNER_DISCONNECT_GRACE_S = 10.0). After the
# replacement runner is online, wait past it (plus margin) so the skipped
# offline-marking timer has provably fired and any reconnect-time
# reconciliation a fixed build performs has had time to settle.
_SETTLE_AFTER_RECONNECT_S = 15.0

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_TIMEOUT_S = 90.0
_LOOPBACK_NO_PROXY = "localhost,127.0.0.1"

pytestmark = [pytest.mark.timeout(600, method="signal")]


def _ambient_free_environ() -> dict[str, str]:
    """Return ``os.environ`` minus ambient runner/host identity variables.

    When the test itself runs inside an omnigent runner (an agent session),
    the parent process leaks ``OMNIGENT_RUNNER_*`` / ``OMNIGENT_HOST_*`` vars
    that make the dedicated stack's subprocesses bind to the *outer* server
    instead of this test's own. Strip them so the stack is hermetic.

    :returns: A copy of the environment safe to base subprocess envs on.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        and k not in ("RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN")
    }


def _merged_no_proxy(env: dict[str, str]) -> str:
    """Return the env's NO_PROXY extended with loopback hosts.

    :param env: Environment mapping about to be passed to a subprocess.
    :returns: Comma-joined NO_PROXY value including loopback entries.
    """
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    parts = [p for p in existing.split(",") if p]
    for host in _LOOPBACK_NO_PROXY.split(","):
        if host not in parts:
            parts.append(host)
    return ",".join(parts)


class _RunnerRestartStack:
    """A dedicated server plus a killable, replaceable runner.

    The shared session-scoped ``live_server`` fixture's runner cannot be
    killed mid-test without poisoning every other test, so this stack owns
    its own subprocesses. The server stays up throughout; the runner can be
    SIGKILLed and respawned with the same tunnel binding token, so the
    replacement reconnects as the *same* runner id — the mid-turn
    interruption this bug needs.
    """

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
        # Base on the server env so the runner imports the worktree source
        # (PYTHONPATH), mirroring the live_server fixture's runner spawn.
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
        """Return whether the server currently sees the runner online.

        :returns: The ``online`` flag from ``GET /v1/runners/{id}/status``,
            or ``False`` on a transient error.
        """
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
        """Wait for the server health check AND the runner to be online.

        :param timeout: Max seconds to wait before failing the test.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{self.base_url}/health", timeout=2, trust_env=False)
                if health.status_code == 200 and self.runner_online():
                    return
            except httpx.HTTPError:
                # Expected while the stack is still booting; keep polling
                # until the deadline.
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
        """Spawn a fresh runner process with the same binding token.

        The replacement registers under the same runner id, modeling the
        interrupted runner coming back after a connectivity blip / host
        daemon restart. Waits until the server reports it online.

        :param timeout: Max seconds to wait for the reconnect.
        """
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
    """Yield a server + killable-runner stack wired to the mock LLM.

    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :param tmp_path: Per-test temp dir for DB, artifacts, and logs.
    :returns: The started :class:`_RunnerRestartStack`.
    """
    stack = _RunnerRestartStack(mock_llm_server_url, tmp_path)
    stack.start()
    try:
        yield stack
    finally:
        stack.teardown()


def _create_session(client: httpx.Client, agent_name: str, runner_id: str) -> str:
    """Create a session for *agent_name* bound to *runner_id*.

    :param client: HTTP client pointed at the stack's server.
    :param agent_name: Registered agent display name.
    :param runner_id: The stack's runner id.
    :returns: The session/conversation id.
    """
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
    """POST a plain user message to *session_id*.

    :param client: HTTP client pointed at the stack's server.
    :param session_id: Target session id.
    :param text: Message text.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    resp.raise_for_status()


def _session_blob(client: httpx.Client, session_id: str) -> str:
    """Return the session snapshot's items as one JSON string.

    :param client: HTTP client pointed at the stack's server.
    :param session_id: Target session id.
    :returns: JSON-dumped items list, or ``""`` on a transient error.
    """
    try:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
    except httpx.HTTPError:
        return ""
    return json.dumps(resp.json().get("items", []))


def _session_status(client: httpx.Client, session_id: str) -> str:
    """Return the status field the supervisor's info tool projects.

    This mirrors the runner's ``sys_session_get_info`` read exactly:
    ``GET /v1/sessions/{id}`` with items and liveness skipped, projecting
    ``status`` from the snapshot.

    :param client: HTTP client pointed at the stack's server.
    :param session_id: Target session id.
    :returns: The snapshot's ``status`` string, or ``""`` on error.
    """
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
    """Return the sub-agent child session id of *parent_session_id*, if any.

    :param client: HTTP client pointed at the stack's server.
    :param parent_session_id: The supervisor session id.
    :returns: The child session id, or ``None`` when not yet created.
    """
    resp = client.get("/v1/sessions", params={"kind": "sub_agent", "limit": 50})
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
    """Poll *condition* until true or fail the test after *timeout* seconds.

    :param condition: Zero-arg callable returning the current truth.
    :param timeout: Max seconds to wait.
    :param what: Failure description for the assertion message.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.25)
    raise AssertionError(f"Timed out after {timeout}s waiting for: {what}")


def _gate_pending(mock_url: str) -> bool:
    """Return whether a mock-LLM request is currently blocked on a gate.

    :param mock_url: Mock LLM server base URL.
    :returns: True when a gated request is waiting.
    """
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _release_gate(mock_url: str) -> None:
    """Release the oldest pending mock-LLM gate.

    :param mock_url: Mock LLM server base URL.
    """
    resp = httpx.post(f"{mock_url}/gate/release", timeout=5, trust_env=False)
    resp.raise_for_status()


def _statuses_in_payload(payload: object, session_id: str) -> list[str]:
    """Collect ``status`` values from dicts that reference *session_id*.

    Walks an arbitrary JSON payload (a tool result of any shape) and
    returns the ``status`` of every dict that carries *session_id* as one
    of its string values — i.e. the rows a supervisor's session-listing /
    info tool reported for that session.

    :param payload: Decoded JSON payload to walk.
    :param session_id: The session id to look for.
    :returns: Every status string found alongside the id.
    """
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
    """Return the raw status-check tool outputs the supervisor's LLM saw.

    Scans the mock server's captured requests for the parent model and
    collects every ``function_call_output`` string for the scripted
    status-check tool call. This is the supervisor's ground truth: whatever
    appears here is what the model reasons from.

    :param mock_url: Mock LLM server base URL.
    :param parent_key: The parent model key on the mock server.
    :param call_id: The scripted status-check tool call id.
    :returns: Every raw output string captured for the call.
    """
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
    """Decode a captured tool-output string into a payload to walk.

    The runner emits tool results as JSON, but a harness may re-serialize
    the parsed dict when building the ``function_call_output`` it sends the
    LLM (Python ``str(dict)`` — single quotes), so accept both encodings.

    :param output: Raw ``function_call_output`` string.
    :returns: The decoded payload, or ``None`` when neither decode works.
    """
    try:
        return json.loads(output)
    except ValueError:
        pass
    try:
        return ast.literal_eval(output)
    except (ValueError, SyntaxError):
        return None


def _supervisor_reported_statuses(outputs: list[str], child_id: str) -> list[str]:
    """Extract the statuses reported for *child_id* from raw tool outputs.

    :param outputs: Raw ``function_call_output`` strings.
    :param child_id: The child session id to look for.
    :returns: Every status string found alongside the id.
    """
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
    """A dead sub-agent turn must not read as still running post-reconnect.

    Journey: the supervisor dispatches a researcher sub-agent and goes
    idle; the runner process is killed mid-child-turn and a replacement
    reconnects under the same runner id (a connectivity interruption); the
    user then asks the supervisor to check on the sub-agent. The status
    the supervisor's inspection tool reports for the child — whose turn
    died with the old runner and can never complete — must not be
    ``running``/``waiting``.
    """
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

    # Supervisor queue: dispatch → ack text → (on the status bump)
    # sys_session_list → text. The tool output captured between the last
    # two responses is exactly what the supervisor model is told about its
    # sub-agents.
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
    # Child queue: ONE gated response — the child stays mid-LLM-call
    # (status ``running``) until the test releases the gate, which it does
    # only after the runner holding the turn has been killed.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Research complete. {_CHILD_COMPLETION}", "block": True}],
        key=child_model,
    )

    session_id = _create_session(client, parent_name, stack.runner_id)
    _post_user_message(client, session_id, "Dispatch the researcher sub-agent.")

    # Supervisor dispatch turn ends (ack text visible); the child is
    # mid-LLM-call, parked on the mock gate.
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

    # The interruption: the runner process dies mid-child-turn and a
    # replacement reconnects under the same runner id, inside the server's
    # disconnect grace. The child's in-flight turn is gone for good — its
    # sole scripted completion is released into the dead process below.
    stack.kill_runner()
    stack.spawn_replacement_runner()
    _release_gate(mock_llm_server_url)

    # Let the disconnect grace elapse and any reconnect-time reconciliation
    # settle before the user asks for a status update.
    time.sleep(_SETTLE_AFTER_RECONNECT_S)

    # Ground truth: the child never completed (no completion marker in its
    # transcript) and never will — its turn died with the old runner.
    child_completed = _CHILD_COMPLETION in _session_blob(client, child_id)

    # The user asks the supervisor to check on the sub-agent; the
    # supervisor inspects via sys_session_list.
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
