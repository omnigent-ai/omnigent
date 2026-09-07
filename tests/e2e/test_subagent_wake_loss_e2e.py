"""End-to-end test: a sub-agent completion must survive a wake-POST outage.

A parent orchestrator dispatches a sub-agent and goes idle. When the child
finishes, the runner delivers the result into the parent's inbox and POSTs a
``[System: ... waiting in inbox]`` wake notice to the parent's event stream —
the wake is the *sole* delivery signal that makes the parent surface the
result without further user input.

If the Omnigent server is briefly unreachable at child-completion time (a
routine tunnel reconnect, a server redeploy), the wake POST exhausts its
bounded retry budget and gives up. The result then sits stranded in the
parent's inbox forever: no later reconnect or turn re-attempts the wake, so
the user only learns the sub-agent finished by manually bumping the parent
("Status update on subagents") — which drains the inbox and reveals the
long-completed work.

This test reproduces that journey deterministically:

1. The parent dispatches a gated researcher sub-agent (its mock-LLM response
   blocks until the test releases a gate), then ends its turn.
2. The test kills the server while the child is mid-LLM-call, releases the
   gate so the child completes into a dead server, and waits for the runner
   to log that the wake POST failed.
3. The test restarts the server (same DB/port/tunnel token; the runner's
   tunnel reconnects) and asserts the child's completion still auto-surfaces
   in the parent session — with NO user bump.

On a build with the stranding bug, no wake ever arrives after the restart and
the assertion fails; the failure message includes proof that the payload was
deliverable all along (a manual bump drains it via ``sys_read_inbox``).

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke
with::

    pytest tests/e2e/test_subagent_wake_loss_e2e.py -v --timeout=600
"""

from __future__ import annotations

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

# The auto-wake notice is the ONLY place this substring is emitted
# (``_format_subagent_wake_notice``); the sys_read_inbox drain message does
# not contain it, so its presence is an auto-wake-specific signal.
_WAKE_NOTICE_SIGNATURE = "waiting in inbox"
_RESEARCHER_MARKER = "RESEARCHER_MARKER_WAKE_LOSS"
_DISPATCH_ACK = "DISPATCH_ACK_WAKE_LOSS"
_INBOX_CHECKED = "INBOX_CHECKED_WAKE_LOSS"
# Warning the runner logs when the wake POST exhausts its bounded retries.
_WAKE_FAILED_LOG = "Sub-agent wake POST failed"

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


class _WakeLossStack:
    """A dedicated, restartable server + runner pair for this test.

    The shared session-scoped ``live_server`` fixture cannot be killed
    mid-test without poisoning every other test, so this stack owns its own
    subprocesses. The server can be killed and restarted on the same port,
    database, and tunnel binding token; the runner keeps retrying its tunnel
    with capped backoff and reconnects to the restarted server.
    """

    def __init__(self, mock_llm_server_url: str, tmp_path: Path) -> None:
        self._mock_base = f"{mock_llm_server_url}/v1"
        self._tmp_path = tmp_path
        self._port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self._port}"
        self._db_path = tmp_path / "wake_loss.db"
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
        self._server_log_handle = open(self.server_log, "a")  # noqa: SIM115 — lives for the Popen lifetime; closed in teardown/kill
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

    def wait_healthy(self, timeout: float = _HEALTH_TIMEOUT_S) -> None:
        """Wait for the server health check AND the runner to be online.

        :param timeout: Max seconds to wait before failing the test.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{self.base_url}/health", timeout=2, trust_env=False)
                status = httpx.get(
                    f"{self.base_url}/v1/runners/{self.runner_id}/status",
                    timeout=2,
                    trust_env=False,
                )
                if (
                    health.status_code == 200
                    and status.status_code == 200
                    and status.json()["online"] is True
                ):
                    return
            except httpx.HTTPError:
                # Expected while the stack is still booting or mid-restart;
                # fall through to the paced poll below and retry until the
                # deadline. NOT `continue`: that would skip the sleep and
                # busy-spin against the half-up server.
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

    def kill_server(self) -> None:
        """SIGKILL the server and wait until its port refuses connections."""
        assert self._server_proc is not None
        self._server_proc.kill()
        self._server_proc.wait(timeout=10)
        if self._server_log_handle is not None:
            self._server_log_handle.close()
            self._server_log_handle = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{self.base_url}/health", timeout=1, trust_env=False)
            except httpx.HTTPError:
                return
            time.sleep(0.2)
        raise RuntimeError("Server port still accepting connections after kill.")

    def restart_server(self) -> None:
        """Restart the server on the same port/DB/token and wait for health."""
        self._spawn_server()
        self.wait_healthy()

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
def wake_loss_stack(
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[_WakeLossStack]:
    """Yield a restartable server+runner stack wired to the mock LLM.

    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :param tmp_path: Per-test temp dir for DB, artifacts, and logs.
    :returns: The started :class:`_WakeLossStack`.
    """
    stack = _WakeLossStack(mock_llm_server_url, tmp_path)
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

    Tolerates transient connection errors (the server restarts mid-test).

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


def _poll_until_soft(condition: Callable[[], bool], timeout: float) -> bool:
    """Poll *condition* until true; return False instead of failing.

    :param condition: Zero-arg callable returning the current truth.
    :param timeout: Max seconds to wait.
    :returns: Whether the condition became true within the deadline.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.25)
    return False


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


def _wait_for_log_line(log_path: Path, needle: str, timeout: float) -> bool:
    """Wait for *needle* to appear in *log_path*.

    :param log_path: Log file to scan.
    :param needle: Substring to look for.
    :param timeout: Max seconds to wait.
    :returns: Whether the line appeared within the deadline.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists() and needle in log_path.read_text(errors="replace"):
            return True
        time.sleep(0.5)
    return False


def _captured_requests_blob(mock_url: str, key: str) -> str:
    """Return the mock server's captured requests for *key* as JSON text.

    :param mock_url: Mock LLM server base URL.
    :param key: Model key whose captured requests to fetch.
    :returns: JSON-dumped captured request bodies.
    """
    resp = httpx.get(f"{mock_url}/mock/requests", params={"key": key}, timeout=5, trust_env=False)
    resp.raise_for_status()
    return json.dumps(resp.json().get("requests", []))


def test_subagent_completion_survives_wake_post_outage(
    wake_loss_stack: _WakeLossStack,
    mock_llm_server_url: str,
) -> None:
    """A sub-agent result must auto-surface even when the wake POST first fails.

    Journey: the parent dispatches a researcher sub-agent and goes idle; the
    server restarts while the researcher finishes (so the runner's wake POST
    fails its bounded retries); after the server is back and the runner's
    tunnel has reconnected, the completion must still reach the parent — the
    wake notice must appear in the parent session with NO user bump.
    """
    stack = wake_loss_stack
    client = stack.client
    reset_mock_llm(mock_llm_server_url)

    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-wl-parent-{uid}"
    child_model = f"mock-wl-child-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"

    parent_name = register_inline_agent(
        client,
        name=f"wl-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are the wake-loss E2E test fixture parent. Dispatch the "
            "researcher sub-agent via sys_session_send when asked, and read "
            "your inbox when woken."
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

    # Parent queue: dispatch → ack text → (on wake or bump) read inbox → text.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "researcher",
                                "title": "wake-loss",
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
                        "call_id": "call_drain",
                        "name": "sys_read_inbox",
                        "arguments": "{}",
                    }
                ],
            },
            {"text": f"{_INBOX_CHECKED}: drained the inbox."},
        ],
        key=parent_model,
    )
    # Child queue: ONE gated response — the child completes only when the
    # test releases the gate, which it does while the server is down.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Research complete. {_RESEARCHER_MARKER}", "block": True}],
        key=child_model,
    )

    session_id = _create_session(client, parent_name, stack.runner_id)
    _post_user_message(client, session_id, "Dispatch the researcher sub-agent.")

    # Parent dispatch turn ends (ack text visible); child is mid-LLM-call,
    # parked on the mock gate.
    _poll_until(
        lambda: _DISPATCH_ACK in _session_blob(client, session_id),
        timeout=120,
        what="parent dispatch turn to complete (ack text in session items)",
    )
    _poll_until(
        lambda: _gate_pending(mock_llm_server_url),
        timeout=60,
        what="the researcher child to reach its gated LLM call",
    )

    # The outage: the server dies, THEN the child completes into it. The
    # runner delivers the result to the parent inbox locally and its wake
    # POST fails every bounded retry.
    stack.kill_server()
    _release_gate(mock_llm_server_url)
    wake_failed_logged = _wait_for_log_line(stack.runner_log, _WAKE_FAILED_LOG, timeout=45)

    # Recovery: the server returns on the same port/DB; the runner's tunnel
    # reconnects. From here the test sends NOTHING — a correct build must
    # still deliver the child's completion to the parent.
    stack.restart_server()

    auto_woken = _poll_until_soft(
        lambda: _WAKE_NOTICE_SIGNATURE in _session_blob(client, session_id),
        timeout=90,
    )

    workaround_note = "not attempted"
    if not auto_woken:
        # Diagnostic only: prove the payload sat deliverable in the inbox all
        # along — a manual user bump drains it via sys_read_inbox.
        _post_user_message(client, session_id, "Status update on subagents.")
        bumped = _poll_until_soft(
            lambda: _INBOX_CHECKED in _session_blob(client, session_id),
            timeout=90,
        )
        marker_in_drain = _RESEARCHER_MARKER in _captured_requests_blob(
            mock_llm_server_url, parent_model
        )
        workaround_note = (
            f"manual bump drained the inbox: bump_turn_ran={bumped}, "
            f"stranded child result present in sys_read_inbox drain={marker_in_drain}"
        )

    assert auto_woken, (
        f"Sub-agent completion was permanently stranded in session {session_id}: "
        f"no auto-wake notice ({_WAKE_NOTICE_SIGNATURE!r}) appeared within 90s of "
        f"the server coming back, even though the runner reconnected. "
        f"wake POST failure logged by runner={wake_failed_logged}; "
        f"workaround evidence: {workaround_note}. The wake POST is the sole "
        f"delivery signal, and nothing re-attempts it after its bounded retries "
        f"are exhausted — the user must manually bump the parent to learn the "
        f"sub-agent finished."
    )
