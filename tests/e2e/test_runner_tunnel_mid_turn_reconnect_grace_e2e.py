"""E2E: tunnel reconnections preserve in-flight work on the same or a new replica.

Real server and runner processes communicate through the production WebSocket
tunnel and a TCP ingress proxy. A deterministic mock LLM holds an openai-agents
turn in flight while the proxy cuts the tunnel and returns HTTP 503. The runner
then reconnects to the original server or a second server sharing its database.

The cross-replica case also covers an unavailable conversation database on the
old replica, with metadata in a separate database. Only queries on A's
conversation engine are fault-injected; the servers, runner, WebSocket
reconnection, shared heartbeat writes, and turn execution are real.

Run::

    uv run --no-sync pytest \
        tests/e2e/test_runner_tunnel_mid_turn_reconnect_grace_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_runner_env, apply_server_env
from tests.e2e.conftest import (
    _REPO_ROOT,
    configure_mock_llm,
    create_runner_bound_session,
    find_free_port,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
)

# Exercise a substantial outage within the 90-second runner liveness lease.
_BLACKOUT_S = 45.0
_ANSWER = "RUNNER_RECONNECT_GRACE_E2E_COMPLETED"
# Both server paths that fail a turn on a runner drop log through the same
# "session turn failed" line: the relay give-up stamps
# origin=runner_disconnected_mid_turn and the per-runner disconnect timer stamps
# origin=runner_offline_sweep. Reject either, so a regression in one path cannot
# hide behind the other.
_FAILURE_SIGNATURE = re.compile(
    r"session turn failed for \S+ \(origin=\S+ code=runner_disconnected"
)
_HEALTH_TIMEOUT_S = 90.0

# Only replica A uses this bootstrap; the file arms its backend outage after
# the real runner has reconnected to B. The metadata database remains readable.
_UNAVAILABLE_BACKEND_BOOTSTRAP = """
import sys
from pathlib import Path

import grpc
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url

fault_file = Path(sys.argv.pop(1))
conversation_database = make_url(sys.argv.pop(1))

class BackendUnavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "injected conversation backend outage"

    def __str__(self):
        return f"{self.code()}: {self.details()}"

@event.listens_for(Engine, "before_cursor_execute")
def fail_conversation_query(connection, cursor, statement, parameters, context, executemany):
    if connection.engine.url == conversation_database and fault_file.exists():
        raise BackendUnavailable()

from omnigent.cli import main

main()
"""

pytestmark = [pytest.mark.timeout(420, method="signal")]


def _ambient_free_environ() -> dict[str, str]:
    """Return an environment without an outer Omnigent session identity."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        and key not in ("RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN")
    }
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = env["NO_PROXY"]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM then SIGKILL for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _poll_until(condition: Callable[[], bool], *, timeout: float, what: str) -> None:
    """Poll *condition* until it succeeds or raise a useful failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(f"Timed out after {timeout:.0f}s waiting for {what}")


class _TunnelIngressProxy:
    """TCP proxy that can cut live sockets and temporarily answer HTTP 503."""

    def __init__(self, backend_host: str, backend_port: int) -> None:
        self._backend = (backend_host, backend_port)
        self._lock = threading.Lock()
        self._reject = False
        self._live_sockets: set[socket.socket] = set()
        self.rejected_connections = 0
        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._live_sockets.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._live_sockets.discard(sock)

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()

    def _pipe(self, source: socket.socket, destination: socket.socket) -> None:
        try:
            while chunk := source.recv(65536):
                destination.sendall(chunk)
        except OSError:
            # Expected when begin_blackout() or teardown cuts a socket mid-pipe;
            # the finally block closes both ends either way.
            return
        finally:
            self._close_socket(source)
            self._close_socket(destination)

    def _handle(self, client: socket.socket) -> None:
        self._track(client)
        with self._lock:
            reject = self._reject
            if reject:
                self.rejected_connections += 1
        if reject:
            with contextlib.suppress(OSError):
                client.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            self._close_socket(client)
            self._untrack(client)
            return

        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection(self._backend, timeout=5.0)
            upstream.settimeout(None)
            client.settimeout(None)
            self._track(upstream)
            forward = threading.Thread(
                target=self._pipe,
                args=(client, upstream),
                daemon=True,
            )
            forward.start()
            self._pipe(upstream, client)
            forward.join(timeout=1.0)
        except OSError:
            self._close_socket(client)
            if upstream is not None:
                self._close_socket(upstream)
        finally:
            self._untrack(client)
            if upstream is not None:
                self._untrack(upstream)

    def begin_blackout(self) -> None:
        """Reject new handshakes and sever every established tunnel socket."""
        with self._lock:
            self._reject = True
            sockets = list(self._live_sockets)
        for sock in sockets:
            self._close_socket(sock)

    def end_blackout(self) -> None:
        """Resume byte-for-byte forwarding to the real server."""
        with self._lock:
            self._reject = False

    def retarget(self, backend_host: str, backend_port: int) -> None:
        """Forward new connections to a different server replica."""
        with self._lock:
            self._backend = (backend_host, backend_port)

    def close(self) -> None:
        """Stop the proxy and close all active sockets."""
        self.end_blackout()
        with self._lock:
            sockets = list(self._live_sockets)
        for sock in sockets:
            self._close_socket(sock)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


class _ReconnectStack:
    """Dedicated server, ingress proxy, and runner for the reconnect journey."""

    def __init__(
        self,
        mock_llm_server_url: str,
        tmp_path: Path,
        *,
        fail_conversation_reads: bool = False,
    ) -> None:
        self._mock_base = f"{mock_llm_server_url}/v1"
        self._port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self._port}"
        self._binding_token = uuid.uuid4().hex + uuid.uuid4().hex
        self.runner_id = token_bound_runner_id(self._binding_token)
        self._database_uri = f"sqlite:///{tmp_path / 'reconnect.db'}"
        self._conversation_database_uri = f"sqlite:///{tmp_path / 'conversations.db'}"
        self._artifact_dir = tmp_path / "artifacts"
        self._artifact_dir.mkdir()
        self.server_log = tmp_path / "server.log"
        # The server's own log file. Its stderr mirroring is env-dependent,
        # so assertions on server log lines read this path, which
        # configure_process_logging always writes when the variable is set.
        self.process_log = tmp_path / "server-process.log"
        self.runner_log = tmp_path / "runner.log"
        self.conversation_read_fault = (
            tmp_path / "conversation-backend-unavailable" if fail_conversation_reads else None
        )
        self._server_handle = self.server_log.open("w")
        self._runner_handle = self.runner_log.open("w")
        self._server_proc: subprocess.Popen[bytes] | None = None
        self._runner_proc: subprocess.Popen[bytes] | None = None
        self.proxy: _TunnelIngressProxy | None = None
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0, trust_env=False)

    def _server_env(self, process_log: Path | None = None) -> dict[str, str]:
        env = {
            **_ambient_free_environ(),
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": self._mock_base,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": self._binding_token,
            "OMNIGENT_PROCESS_LOG_FILE": str(process_log or self.process_log),
        }
        apply_server_env(env, _REPO_ROOT)
        return env

    def start(self) -> None:
        """Start the server, proxy, and runner, then wait for registration."""
        entrypoint = ["-m", "omnigent.cli"]
        if self.conversation_read_fault is not None:
            entrypoint = [
                "-c",
                _UNAVAILABLE_BACKEND_BOOTSTRAP,
                str(self.conversation_read_fault),
                self._conversation_database_uri,
            ]
        self._server_proc = subprocess.Popen(
            [
                sys.executable,
                *entrypoint,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self._port),
                "--database-uri",
                self._database_uri,
                "--conversation-database-uri",
                self._conversation_database_uri,
                "--artifact-location",
                str(self._artifact_dir),
            ],
            env=self._server_env(),
            stdout=self._server_handle,
            stderr=subprocess.STDOUT,
        )
        _poll_until(
            self._server_healthy,
            timeout=_HEALTH_TIMEOUT_S,
            what="the real Omnigent server to become healthy",
        )
        self.proxy = _TunnelIngressProxy("127.0.0.1", self._port)
        proxy_url = f"http://127.0.0.1:{self.proxy.port}"
        runner_env = apply_runner_env(
            {
                **{
                    k: v for k, v in self._server_env().items() if k != "OMNIGENT_PROCESS_LOG_FILE"
                },
                "OMNIGENT_RUNNER_ID": self.runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": self._binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": proxy_url,
            }
        )
        self._runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=self._runner_handle,
            stderr=subprocess.STDOUT,
        )
        self.wait_runner_online()

    def _server_healthy(self) -> bool:
        try:
            return self.client.get("/health", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def _runner_online(self) -> bool:
        try:
            response = self.client.get(f"/v1/runners/{self.runner_id}/status", timeout=2.0)
            return response.status_code == 200 and response.json().get("online") is True
        except httpx.HTTPError:
            return False

    def wait_runner_online(self) -> None:
        """Wait until the server sees the real runner tunnel as online."""
        _poll_until(
            self._runner_online,
            timeout=_HEALTH_TIMEOUT_S,
            what="the runner WebSocket tunnel to register",
        )

    def start_second_replica(self) -> _Replica:
        """Start a second server replica sharing this stack's database."""
        replica = _Replica(self, self.server_log.parent)
        replica.start()
        return replica

    def teardown(self) -> None:
        """Stop processes, proxy, HTTP client, and log handles."""
        _terminate(self._runner_proc)
        if self.proxy is not None:
            self.proxy.close()
        _terminate(self._server_proc)
        self.client.close()
        self._runner_handle.close()
        self._server_handle.close()


class _Replica:
    """A second real server process over the same database as a stack."""

    def __init__(self, stack: _ReconnectStack, log_dir: Path) -> None:
        self._stack = stack
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server_log = log_dir / "server-b.log"
        self.process_log = log_dir / "server-b-process.log"
        self._handle = self.server_log.open("w")
        self._proc: subprocess.Popen[bytes] | None = None
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0, trust_env=False)

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--database-uri",
                self._stack._database_uri,
                "--conversation-database-uri",
                self._stack._conversation_database_uri,
                "--artifact-location",
                str(self._stack._artifact_dir),
            ],
            env=self._stack._server_env(process_log=self.process_log),
            stdout=self._handle,
            stderr=subprocess.STDOUT,
        )
        _poll_until(
            self._healthy,
            timeout=_HEALTH_TIMEOUT_S,
            what="the second Omnigent replica to become healthy",
        )

    def _healthy(self) -> bool:
        try:
            return self.client.get("/health", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def runner_online(self) -> bool:
        try:
            response = self.client.get(f"/v1/runners/{self._stack.runner_id}/status", timeout=2.0)
            return response.status_code == 200 and response.json().get("online") is True
        except httpx.HTTPError:
            return False

    def teardown(self) -> None:
        _terminate(self._proc)
        self.client.close()
        self._handle.close()


@pytest.fixture
def reconnect_stack(
    mock_llm_server_url: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[_ReconnectStack]:
    """Yield a dedicated real server/runner stack behind a fault proxy."""
    stack = _ReconnectStack(
        mock_llm_server_url,
        tmp_path,
        fail_conversation_reads=getattr(request, "param", False),
    )
    stack.start()
    try:
        yield stack
    finally:
        stack.teardown()


def _gate_pending(mock_url: str) -> bool:
    response = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
    response.raise_for_status()
    return bool(response.json().get("pending"))


def _session_snapshot(client: httpx.Client, session_id: str) -> dict:
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json()


def _session_list_entry(client: httpx.Client, session_id: str) -> dict:
    response = client.get(
        "/v1/sessions",
        params={"visibility": "all", "limit": 100},
    )
    response.raise_for_status()
    return next(entry for entry in response.json()["data"] if entry["id"] == session_id)


def _session_blob(client: httpx.Client, session_id: str) -> str:
    return json.dumps(_session_snapshot(client, session_id).get("items", []))


def _send_user_message(
    client: httpx.Client,
    session_id: str,
    text: str = "Finish after reconnecting.",
) -> None:
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    )
    response.raise_for_status()


def test_mid_turn_tunnel_blackout_recovers_without_failed_edge(
    reconnect_stack: _ReconnectStack,
    mock_llm_server_url: str,
) -> None:
    """A 45-second outage must recover without failure, lost output or relay polling."""
    stack = reconnect_stack
    proxy = stack.proxy
    assert proxy is not None
    reset_mock_llm(mock_llm_server_url)
    model = f"runner-reconnect-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _ANSWER, "block": True}],
        key=model,
    )
    agent_name = register_inline_agent(
        stack.client,
        name=f"runner-reconnect-{uuid.uuid4().hex[:8]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Return the configured answer.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    session_id = create_runner_bound_session(
        stack.client,
        agent_name=agent_name,
        runner_id=stack.runner_id,
    )
    _send_user_message(stack.client, session_id)
    _poll_until(
        lambda: _gate_pending(mock_llm_server_url),
        timeout=60.0,
        what="the real turn to block inside the mock LLM",
    )

    blackout_started = time.monotonic()
    proxy.begin_blackout()
    try:
        _poll_until(
            lambda: proxy.rejected_connections > 0,
            timeout=10.0,
            what="the real runner to attempt a reconnect through the 503 ingress",
        )
        remaining = _BLACKOUT_S - (time.monotonic() - blackout_started)
        if remaining > 0:
            time.sleep(remaining)
    finally:
        proxy.end_blackout()

    stack.wait_runner_online()
    release_mock_gate(mock_llm_server_url)
    _poll_until(
        lambda: _ANSWER in _session_blob(stack.client, session_id),
        timeout=60.0,
        what="the original in-flight turn to complete after runner reconnect",
    )

    server_log = stack.process_log.read_text()
    assert _FAILURE_SIGNATURE.search(server_log) is None, (
        f"The server failed session {session_id} during a {_BLACKOUT_S:.0f}s transient "
        "runner-tunnel outage even though the same runner reconnected and the original "
        "turn completed. This reproduces the production false-fatal path.\n"
        f"Rejected reconnect handshakes: {proxy.rejected_connections}.\n"
        f"Server log tail:\n{server_log[-5000:]}"
    )

    # One transport-lost row proves the outage occurred; a polling relay then
    # logs a retry per attempt. Count both in the server's own log file.
    outages = server_log.count(f"Relay: runner transport lost for session={session_id} (")
    assert outages >= 1, "the blackout never registered as a transport loss in the server log"
    retry_lines = [
        line
        for line in server_log.splitlines()
        if f"transport lost for session={session_id}; retrying" in line
    ]
    assert len(retry_lines) <= 1, (
        f"The relay re-opened GET /stream {len(retry_lines)} times during a single "
        f"{_BLACKOUT_S:.0f}s outage instead of waiting once for the runner to re-register."
    )

    # The failed edge is an SSE event that vanishes on reload; the durable
    # proof is the reload-visible snapshot. A recovered session must read a
    # non-failed status and carry no persisted runner_disconnected label, or
    # the user reopens the session to a red card with a Retry button next to
    # a turn that actually finished.
    snapshot = _session_snapshot(stack.client, session_id)
    assert snapshot.get("status") != "failed", (
        f"Session {session_id} reads 'failed' after a transient outage it recovered from; "
        f"snapshot={json.dumps(snapshot)[:2000]}"
    )
    snapshot_text = json.dumps(snapshot)
    assert "runner_disconnected" not in snapshot_text, (
        f"Session {session_id} still carries a persisted runner_disconnected label after "
        f"recovery; snapshot={snapshot_text[:2000]}"
    )


@pytest.mark.parametrize(
    "reconnect_stack",
    [False, True],
    ids=["healthy-store", "unavailable-conversation-backend"],
    indirect=True,
)
def test_reconnect_to_another_replica_does_not_fail_the_turn(
    reconnect_stack: _ReconnectStack,
    mock_llm_server_url: str,
) -> None:
    """A runner that reconnects to a different replica must not be failed by the first.

    Replica A relays the turn. Its tunnel is cut, and the runner reconnects to
    replica B (same database). Keep the turn running beyond A's grace period,
    with A's conversation database queries optionally raising UNAVAILABLE.
    B's shared heartbeat must prevent A from failing the turn, and B must
    deliver both the original answer and a follow-up turn.
    """
    from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S

    stack = reconnect_stack
    proxy = stack.proxy
    assert proxy is not None
    replica_b = stack.start_second_replica()
    try:
        reset_mock_llm(mock_llm_server_url)
        model = f"runner-cross-replica-{uuid.uuid4().hex[:8]}"
        follow_up_answer = f"{_ANSWER}_FOLLOW_UP"
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _ANSWER, "block": True}, {"text": follow_up_answer}],
            key=model,
        )
        agent_name = register_inline_agent(
            stack.client,
            name=f"runner-cross-replica-{uuid.uuid4().hex[:8]}",
            harness="openai-agents",
            model=model,
            profile="",
            prompt="Return the configured answer.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        session_id = create_runner_bound_session(
            stack.client,
            agent_name=agent_name,
            runner_id=stack.runner_id,
        )
        _send_user_message(stack.client, session_id)
        _poll_until(
            lambda: _gate_pending(mock_llm_server_url),
            timeout=60.0,
            what="the real turn to block inside the mock LLM on replica A",
        )

        # Cut A's tunnel, then hand the runner's next connection to B.
        proxy.begin_blackout()
        _poll_until(
            lambda: proxy.rejected_connections > 0,
            timeout=10.0,
            what="the real runner to attempt a reconnect through the 503 ingress",
        )
        proxy.retarget("127.0.0.1", replica_b.port)
        proxy.end_blackout()
        _poll_until(
            replica_b.runner_online,
            timeout=_HEALTH_TIMEOUT_S,
            what="the runner WebSocket tunnel to register on replica B",
        )

        if stack.conversation_read_fault is not None:
            stack.conversation_read_fault.touch()
            response = stack.client.get(f"/v1/sessions/{session_id}")
            assert response.status_code == 500, response.text
            _poll_until(
                lambda: (
                    "StatusCode.UNAVAILABLE: injected conversation backend outage"
                    in stack.process_log.read_text()
                ),
                timeout=10.0,
                what="replica A to log the injected conversation database failure",
            )
        assert _session_snapshot(replica_b.client, session_id).get("status") == "running"

        # Keep the original turn in flight until A makes its disconnect decision.
        recovered = f"Relay: runner transport lost for session={session_id} (live_elsewhere)"
        _poll_until(
            lambda: (
                recovered in stack.process_log.read_text()
                or bool(_FAILURE_SIGNATURE.search(stack.process_log.read_text()))
            ),
            timeout=RUNNER_DISCONNECT_GRACE_S + 30.0,
            what="replica A to resolve the disconnect while the turn is still running on B",
        )

        server_a_log = stack.process_log.read_text()
        failed_edges = _FAILURE_SIGNATURE.findall(server_a_log)
        assert not failed_edges, (
            f"Replica A failed session {session_id} after the runner reconnected to "
            "replica B. The fresh heartbeat B wrote must remain readable even when "
            "A's conversation database is unavailable.\n"
            f"Failure lines: {failed_edges}\n"
            f"Replica A log tail:\n{server_a_log[-4000:]}"
        )
        assert recovered in server_a_log
        if stack.conversation_read_fault is not None:
            stack.conversation_read_fault.unlink()

        release_mock_gate(mock_llm_server_url)
        _poll_until(
            lambda: _ANSWER in _session_blob(replica_b.client, session_id),
            timeout=60.0,
            what="the original in-flight turn to complete via replica B",
        )
        _send_user_message(replica_b.client, session_id, "Continue after switching replicas.")
        _poll_until(
            lambda: follow_up_answer in _session_blob(replica_b.client, session_id),
            timeout=60.0,
            what="a follow-up turn to complete on the same session and runner on replica B",
        )

        snapshot_a = _session_snapshot(stack.client, session_id)
        assert snapshot_a.get("status") != "running", (
            f"Replica A still reports the completed session as running; "
            f"snapshot={json.dumps(snapshot_a)[:2000]}"
        )
        assert snapshot_a.get("active_response_id") is None
        list_entry_a = _session_list_entry(stack.client, session_id)
        assert list_entry_a.get("status") == snapshot_a.get("status"), (
            f"Replica A's list and snapshot disagree after handoff; "
            f"list_entry={json.dumps(list_entry_a)[:2000]}"
        )

        snapshot = _session_snapshot(replica_b.client, session_id)
        assert snapshot.get("status") != "failed", (
            f"Session {session_id} reads failed after completing on replica B; "
            f"snapshot={json.dumps(snapshot)[:2000]}"
        )
        assert "runner_disconnected" not in json.dumps(snapshot)
        assert not _FAILURE_SIGNATURE.search(stack.process_log.read_text())
        assert not _FAILURE_SIGNATURE.search(replica_b.process_log.read_text())
    finally:
        replica_b.teardown()
