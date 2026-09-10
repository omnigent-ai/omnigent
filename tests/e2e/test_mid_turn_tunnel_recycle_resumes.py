"""E2E regression test: a mid-turn tunnel recycle must resume, not abort.

Reproduces the reported failure: a websocket-tunnel close between the runner
and the server (a routine server-side recycle -- close 1012 / a MAS pod
recycle) aborts the in-flight turn the user is waiting on, surfacing as
``session turn failed: Runner disconnected unexpectedly.`` -- even though the
runner reconnects within a second and its turn is still running.

The failing sites are the server's two disconnect publishers (the per-session
relay supervisor and the runner-disconnect grace timer): both used to race a
fixed reconnect grace against the runner's reconnect and publish the hard
failure whenever the reconnect was slower, so a benign recycle read to the
user as a failed turn instead of being held across the reconnect.

Journey (user-observable):

  1. A host/runner is online and bound to a session.
  2. The user sends a turn; it starts running on the runner (mid-turn).
  3. The runner<->server tunnel is recycled mid-turn (the runner survives and
     keeps retrying -- a server-side pod recycle, NOT a runner death). The
     recycled endpoint takes a little longer than the server's fixed reconnect
     grace to come back (a routine MAS pod cold start / reschedule).
  4. The user's turn fails on their live session stream with "Runner
     disconnected unexpectedly." instead of resuming across the reconnect --
     even though the runner reconnects seconds later and its turn completes.

This stands up a real ``omnigent server`` (posture: accept exactly our
token-bound runner) with a restartable TCP proxy in front of its runner tunnel,
connects a real runner THROUGH the proxy so the tunnel can be severed on demand,
opens a runner-bound session, live-tails that session's client SSE stream the
way the web SPA does, drives a turn that the mock LLM holds open on a gate (so
the turn is genuinely in-flight), recycles the tunnel and holds the endpoint
down a little past the reconnect grace (``RUNNER_DISCONNECT_GRACE_S``), lets the
runner reconnect, releases the gate, and asserts the user's stream did NOT
surface the disconnect failure while the turn's answer still streamed across the
reconnect.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_mid_turn_tunnel_recycle_resumes.py -v
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from omnigent.runner.identity import token_bound_runner_id
from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e.conftest import (
    HEALTH_TIMEOUT_S,
    POLL_INTERVAL_S,
    configure_mock_llm,
    create_runner_bound_session,
    find_free_port,
    poll_session_until_terminal,
    register_inline_agent,
    release_mock_gate,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _RecyclableProxy:
    """A TCP proxy that can recycle its live tunnels on demand.

    While forwarding, every accepted connection is piped byte-for-byte to the
    backend (HTTP and WebSocket alike). :meth:`recycle` severs all live piped
    connections -- exactly what a MAS pod / ingress recycle does to a tunnel --
    while continuing to accept and forward NEW connections immediately, so the
    runner's prompt reconnect lands on a healthy proxy.
    """

    def __init__(self, backend_host: str, backend_port: int) -> None:
        self._backend = (backend_host, backend_port)
        self._live_socks: set[socket.socket] = set()
        self._lock = threading.Lock()
        # When False, newly accepted connections are dropped immediately: the
        # tunnel endpoint is unreachable, modelling a recycled server pod that
        # has not finished coming back up. The runner keeps retrying (~0.5s)
        # but cannot reconnect until :meth:`resume` restores the endpoint.
        self._accepting = True
        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handle(self, client: socket.socket) -> None:
        with self._lock:
            accepting = self._accepting
        if not accepting:
            # Endpoint is down (recycled pod not yet back): refuse the reconnect.
            with contextlib.suppress(OSError):
                client.close()
            return
        try:
            backend = socket.create_connection(self._backend, timeout=10.0)
        except OSError:
            client.close()
            return
        with self._lock:
            self._live_socks.update((client, backend))
        try:
            t1 = threading.Thread(target=self._pipe, args=(client, backend), daemon=True)
            t2 = threading.Thread(target=self._pipe, args=(backend, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            with self._lock:
                self._live_socks.discard(client)
                self._live_socks.discard(backend)
            for sock in (client, backend):
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    def recycle(self, *, block_new: bool = False) -> None:
        """Sever every live tunnel (server-side recycle).

        :param block_new: When ``True``, also refuse new connections until
            :meth:`resume` is called -- the recycled endpoint stays down, so the
            runner's prompt reconnect attempts fail until the pod is back. When
            ``False`` (default), new connections keep being accepted, so the
            runner reconnects immediately onto a healthy proxy.
        """
        with self._lock:
            if block_new:
                self._accepting = False
            socks = list(self._live_socks)
        for sock in socks:
            # shutdown() interrupts blocked recv()s in the pipe threads at once
            # so both the runner and the server see the tunnel die immediately.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def resume(self) -> None:
        """Restore the endpoint so the runner's retries reconnect (pod back up)."""
        with self._lock:
            self._accepting = True

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _SessionStreamViewer:
    """Subscribe to the client-facing session SSE stream, as the web SPA does.

    The web app opens ``GET /v1/sessions/{id}/stream`` (``Accept:
    text/event-stream``) to live-tail a session; every ``session.status`` /
    ``response.*`` event the server publishes reaches the user through it. This
    viewer records the raw frames so the test can assert on what the user
    actually saw. It reconnects on stream close (exactly as the SPA's live-tail
    does), so a terminal ``failed`` event that ends one connection does not stop
    it from also capturing the turn's later resumed output.

    The viewer talks to the server DIRECTLY (not through the recyclable proxy),
    so the recycle severs only the runner tunnel -- the user's stream stays up,
    just like a real browser whose connection to the app is unaffected by a
    backend pod recycling its runner tunnel.
    """

    def __init__(self, base_url: str, session_id: str) -> None:
        self._url = f"{base_url}/v1/sessions/{session_id}/stream"
        self._frames: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=None)
        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=timeout) as sse_client:
                    with sse_client.stream(
                        "GET",
                        self._url,
                        headers={"Accept": "text/event-stream"},
                    ) as resp:
                        self.connected.set()
                        for line in resp.iter_lines():
                            if self._stop.is_set():
                                return
                            if line:
                                with self._lock:
                                    self._frames.append(line)
            except Exception:
                self.connected.set()
            if self._stop.is_set():
                return
            # Brief backoff, then reconnect -- mirrors the SPA's live-tail.
            time.sleep(0.2)

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._frames)

    def wait_for(self, needle: str, timeout: float) -> bool:
        """Return True once *needle* appears in a captured frame, else False."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.text():
                return True
            time.sleep(0.25)
        return False

    def stop(self) -> None:
        self._stop.set()


def _spawn_server(
    *, tmp_path: Path, mock_llm_server_url: str, binding_token: str
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Start an ``omnigent server`` that accepts our own proxied runner.

    Installs the tunnel allow-list token that authorizes exactly the runner we
    spawn ourselves (through the proxy), matching the live E2E server posture.
    Points the LLM and the server-side policy classifier at the mock server.
    """
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "server.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    server_log = tmp_path / "server.log"

    server_cfg = tmp_path / "server.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {
                        "base_url": f"{mock_llm_server_url}/v1",
                        "api_key": "mock-key",
                    },
                }
            }
        )
    )

    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        # Allow-list exactly our proxied runner's binding token.
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
    }
    apply_server_env(env, _REPO_ROOT)

    log_handle = open(server_log, "w")  # noqa: SIM115 -- lives for Popen's lifetime
    proc = subprocess.Popen(
        [
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
            str(server_cfg),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            log_handle.close()
            raise RuntimeError(
                f"server exited early (code {proc.returncode}); "
                f"log tail:\n{server_log.read_text()[-3000:]}"
            )
        time.sleep(POLL_INTERVAL_S)
    else:
        log_handle.close()
        raise RuntimeError(
            f"server didn't pass health within {HEALTH_TIMEOUT_S}s; "
            f"log tail:\n{server_log.read_text()[-3000:]}"
        )

    # The classifier's LLM queue must always ALLOW so the mock never blocks the
    # request-phase policy check for our turn.
    set_fallback_mock_llm(mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}')
    return proc, base_url, server_log


def _spawn_runner(
    *,
    tmp_path: Path,
    dial_url: str,
    mock_llm_server_url: str,
    binding_token: str,
    runner_id: str,
) -> tuple[subprocess.Popen[bytes], Path]:
    """Spawn a real runner that dials *dial_url* (the proxy) with *binding_token*.

    Mirrors the live E2E server fixture's runner wiring: an explicit
    ``OMNIGENT_RUNNER_ID`` (the token-bound id the server allow-lists), the
    binding token, and its process log routed to a file.
    """
    runner_log = tmp_path / "runner.log"
    log_handle = open(runner_log, "w")  # noqa: SIM115 -- lives for Popen's lifetime
    # PYTHONPATH must point at the worktree root so the harness subprocess the
    # runner spawns (``python -m omnigent.runtime.harnesses._runner``) can import
    # ``omnigent`` — its cwd is a transient socket dir, not the repo. This mirrors
    # the live E2E fixture, which runs ``apply_server_env`` before ``apply_runner_env``.
    base_env: dict[str, str] = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": dial_url,
        PROCESS_LOG_FILE_ENV_VAR: str(runner_log),
    }
    apply_server_env(base_env, _REPO_ROOT)
    env = apply_runner_env(base_env)
    proc = subprocess.Popen(
        [runner_executable(), "-m", "omnigent.runner._entry"],
        env=env,
        cwd=compat_runner_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return proc, runner_log


def _wait_runner_online(
    client: httpx.Client, runner_id: str, *, online: bool, timeout: float
) -> None:
    """Poll the DIRECT server URL until *runner_id* reaches the desired online state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            resp = client.get(f"/v1/runners/{runner_id}/status")
            if resp.status_code == 200 and resp.json().get("online") is online:
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"runner {runner_id!r} did not reach online={online} within {timeout}s")


@pytest.mark.timeout(300)
def test_mid_turn_tunnel_recycle_resumes_rather_than_aborts(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A mid-turn tunnel recycle must be ridden out, not failed.

    A server-side tunnel recycle severs the runner<->server tunnel while a turn
    is in flight; the runner survives and keeps reconnecting with an unchanged
    token, and its turn stays fully viable (blocked on the mock gate).

    ``RunnerRegistry.deregister`` aborts everything in flight on the close.
    Pre-fix, the only thing that could save the turn was the relay/disconnect
    reconnect grace (``RUNNER_DISCONNECT_GRACE_S`` = 10s) -- a fixed race: a
    recycled endpoint that takes longer than the grace to accept the reconnect
    (a routine MAS pod cold start / reschedule) exhausted it, and the server
    published a hard ``failed`` -- "Runner disconnected unexpectedly." -- to the
    user's live session stream, even though the runner never died and its turn
    resumes and completes moments later. The fix holds a mid-turn outage for
    ``RUNNER_TURN_RESUME_WINDOW_S`` instead.

    This asserts, on the client-facing SSE stream the web SPA consumes, that the
    user does NOT see the disconnect failure and that the turn's answer streams
    across the reconnect. Pre-fix, the fixed grace lost that race and the
    failure was published; the mid-turn hold across the turn-resume window
    keeps the user's stream clean.
    """
    if mock_llm_server_url is None:
        pytest.skip("requires the mock LLM server (mock mode)")

    marker = f"TUNNEL-RECYCLE-{uuid.uuid4().hex[:8]}"
    binding_token = uuid.uuid4().hex + uuid.uuid4().hex
    runner_id = token_bound_runner_id(binding_token)

    (tmp_path / "srv").mkdir()
    (tmp_path / "run").mkdir()

    server_proc, base_url, server_log = _spawn_server(
        tmp_path=tmp_path / "srv",
        mock_llm_server_url=mock_llm_server_url,
        binding_token=binding_token,
    )
    proxy: _RecyclableProxy | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    runner_log: Path | None = None
    viewer: _SessionStreamViewer | None = None
    try:
        parsed = httpx.URL(base_url)
        proxy = _RecyclableProxy(parsed.host, parsed.port)
        proxy_url = f"http://127.0.0.1:{proxy.port}"

        # Sanity: the server is reachable through the proxy, like a user URL.
        assert httpx.get(f"{proxy_url}/health", timeout=10.0).status_code == 200

        # Client talks to the server DIRECTLY (control plane), so proxy recycles
        # never disturb our REST calls -- only the runner's tunnel is proxied.
        client = httpx.Client(base_url=base_url, timeout=30.0)

        runner_proc, runner_log = _spawn_runner(
            tmp_path=tmp_path / "run",
            dial_url=proxy_url,
            mock_llm_server_url=mock_llm_server_url,
            binding_token=binding_token,
            runner_id=runner_id,
        )
        try:
            _wait_runner_online(client, runner_id, online=True, timeout=45.0)
        except AssertionError as exc:  # pragma: no cover - diagnostics only
            raise AssertionError(
                f"{exc}\n--- runner log ---\n{runner_log.read_text()[-3000:]}\n"
                f"--- server log ---\n{server_log.read_text()[-3000:]}"
            ) from exc

        # A minimal single-model agent that runs on the runner and hits the mock.
        model = f"mock-tunnel-{uuid.uuid4().hex[:6]}"
        agent_name = register_inline_agent(
            client,
            name=f"tunnel-recycle-{uuid.uuid4().hex[:6]}",
            harness="openai-agents",
            model=model,
            profile="",
            prompt="You are a terse smoke-test assistant. Follow the user's instruction exactly.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )

        # The turn's single LLM response blocks on the gate so we can recycle
        # the tunnel while the turn is genuinely in flight on the runner.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": f"resumed across the reconnect: {marker}", "block": True}],
            key=model,
        )

        session_id = create_runner_bound_session(
            client, agent_name=agent_name, runner_id=runner_id
        )

        # Live-tail the session exactly as the web SPA does, so we observe what
        # the user actually sees on their stream across the recycle.
        viewer = _SessionStreamViewer(base_url, session_id)
        viewer.start()
        assert viewer.connected.wait(timeout=15.0), "client SSE stream never connected"

        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content=f"Reply with exactly the literal string {marker} and nothing else.",
        )

        # Wait until the turn is genuinely in flight: the runner's LLM request is
        # blocked on the mock gate.
        try:
            _wait_for_gate_pending(mock_llm_server_url, timeout=60.0)
        except AssertionError as exc:  # pragma: no cover - diagnostics only
            snap = client.get(f"/v1/sessions/{session_id}").json()
            reqs = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=5.0).json()
            raise AssertionError(
                f"{exc}\nsession status={snap.get('status')!r} "
                f"error={snap.get('last_task_error') or snap.get('error')!r}\n"
                f"mock requests={reqs}\n"
                f"--- runner log ---\n{runner_log.read_text()[-3000:]}\n"
                f"--- server log ---\n{server_log.read_text()[-3000:]}"
            ) from exc

        # --- The recycle: sever the live tunnel mid-turn, as a MAS pod / ingress
        # recycle does. The runner process survives and keeps retrying; its turn
        # is still blocked on the gate and fully viable. The recycled endpoint
        # stays down a little longer than the server's fixed reconnect grace
        # (RUNNER_DISCONNECT_GRACE_S = 10s) -- a pod that takes >10s to accept
        # again (scheduling / cold start / image pull), which is routine. The
        # runner's "retrying in ~0.5s" cadence keeps trying throughout. --
        proxy.recycle(block_new=True)
        _wait_runner_online(client, runner_id, online=False, timeout=15.0)
        time.sleep(RUNNER_DISCONNECT_GRACE_S + 3.0)
        proxy.resume()
        _wait_runner_online(client, runner_id, online=True, timeout=45.0)

        # Runner is back with the same id and its turn is still running. Let the
        # LLM finish so the turn can complete across the reconnect.
        release_mock_gate(mock_llm_server_url)

        # Drive the snapshot poll too (the turn does eventually resume + complete
        # server-side even in the buggy case, so this alone would not catch the
        # bug -- the user-visible failure is a transient event on the live
        # stream that the terminal snapshot no longer reflects).
        body = poll_session_until_terminal(
            client,
            session_id=session_id,
            response_id=response_id,
            timeout=90.0,
        )

        # The turn was viable throughout: once the gate releases, its output
        # streams across the reconnect. Waiting for the marker also guarantees
        # the live stream has caught up before we assert on it.
        streamed = viewer.wait_for(marker, timeout=60.0)
        stream_text = viewer.text()

        # THE BUG: a routine tunnel recycle whose reconnect is slower
        # than the server's fixed reconnect grace makes ``deregister`` abort the
        # in-flight turn, and the server publishes a hard failure to the user's
        # live stream -- "Runner disconnected unexpectedly." -- even though the
        # runner never died and its turn resumes and completes moments later.
        # A correct hold-across-reconnect must not surface that to the user.
        assert "Runner disconnected unexpectedly" not in stream_text, (
            "A mid-turn tunnel recycle surfaced a hard 'Runner disconnected "
            "unexpectedly.' failure on the user's live session stream, even "
            "though the runner reconnected and its turn stayed viable. "
            f"Final snapshot status={body['status']!r} "
            f"error={body.get('error')!r}; marker_streamed={streamed}.\n"
            f"Server log tail:\n{server_log.read_text()[-4000:]}"
        )
        # Sanity: the turn genuinely resumed across the reconnect and produced
        # its answer, so the test fails specifically on the spurious disconnect
        # above -- not on an unrelated stall.
        assert streamed, (
            "The turn's answer never streamed across the reconnect "
            f"(marker {marker!r} absent), so the recycle was not ridden out. "
            f"Final snapshot status={body['status']!r} "
            f"error={body.get('error')!r}.\nServer log tail:\n"
            f"{server_log.read_text()[-4000:]}"
        )
    finally:
        if viewer is not None:
            viewer.stop()
        if runner_proc is not None and runner_proc.poll() is None:
            runner_proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                runner_proc.wait(timeout=5)
            if runner_proc.poll() is None:
                runner_proc.kill()
                runner_proc.wait(timeout=5)
        if proxy is not None:
            proxy.close()
        if server_proc is not None and server_proc.poll() is None:
            server_proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                server_proc.wait(timeout=10)
            if server_proc.poll() is None:
                server_proc.kill()
                server_proc.wait(timeout=5)


def _wait_for_gate_pending(mock_llm_server_url: str, timeout: float = 30.0) -> None:
    """Poll until a request is blocked on the mock LLM gate (turn is in flight)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=2.0)
        resp.raise_for_status()
        if resp.json().get("pending"):
            return
        time.sleep(0.1)
    raise AssertionError(f"No gate pending within {timeout}s")
