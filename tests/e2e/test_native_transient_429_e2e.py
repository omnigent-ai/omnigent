"""Retry one transient HTTP 429 on native startup, runner binding and policy checks.

Local fault-injection endpoints exercise real HTTP requests. The startup case
runs the CLI through a proxy to a real server; no provider request is needed."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import click
import httpx
import pytest

from omnigent.native.native_policy_hook import post_evaluate_with_retry
from omnigent.native.native_terminal import bind_session_runner
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable

# Worktree root: tests/e2e/<this file> -> parents[2]. Threaded onto the CLI and
# server subprocesses' PYTHONPATH so they import THIS worktree's code, not a
# stale editable install in the shared .venv.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_SERVER_HEALTH_TIMEOUT_S = 60.0
_CLI_TIMEOUT_S = 150.0
_PTY_ROWS = 50
_PTY_COLS = 200

_RESOURCE_EXHAUSTED_BODY = json.dumps(
    {"error_code": "RESOURCE_EXHAUSTED", "message": "Too many requests; please retry."}
).encode()

# Env vars that, leaked from this (possibly Omnigent-hosted) process into the
# code under test, would mis-route the runner/host or shadow the CLI's own
# state. Stripped from every server/CLI subprocess env.
_STALE_ENV_VARS = (
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "OMNIGENT_RUNNER_WORKSPACE",
    "OMNIGENT_RUNNER_PARENT_PID",
    "OMNIGENT_RUNNER_ISOLATE_SESSION",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
    "OMNIGENT_HOST_NAME",
    "RUNNER_SERVER_URL",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "TMUX",
    "CODEX",
    "CODEX_HOME",
    "DATABRICKS_TOKEN",
)


def _free_port() -> int:
    """Return an unused loopback TCP port."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


# ─────────────────────────── transient-429 endpoint ───────────────────────────


class _Transient429Server(ThreadingHTTPServer):
    """Loopback endpoint that returns 429 once for the selected method and path."""

    daemon_threads = True

    def __init__(
        self,
        *,
        inject_method: str,
        inject_path: str,
        success_status: int,
        success_body: bytes,
    ) -> None:
        self.inject_method = inject_method
        self.inject_path = inject_path
        self.success_status = success_status
        self.success_body = success_body
        self.seen: list[tuple[str, str]] = []
        self._injected = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Transient429Handler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def should_inject(self, method: str, path: str) -> bool:
        with self._lock:
            self.seen.append((method, path))
            if method == self.inject_method and path == self.inject_path and self._injected == 0:
                self._injected += 1
                return True
        return False

    def hits(self, method: str, path: str) -> int:
        with self._lock:
            return sum(1 for m, p in self.seen if m == method and p == path)


class _Transient429Handler(BaseHTTPRequestHandler):
    server: _Transient429Server

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if self.server.should_inject(self.command, self.path):
            self._send(429, _RESOURCE_EXHAUSTED_BODY, extra={"Retry-After": "1"})
            return
        self._send(self.server.success_status, self.server.success_body)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _respond

    def _send(self, status: int, body: bytes, *, extra: dict[str, str] | None = None) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)


# ─────────────────────────── 429-injecting reverse proxy ───────────────────────


class _Transient429Proxy(ThreadingHTTPServer):
    """Forward requests to a real server after returning one targeted 429."""

    daemon_threads = True

    def __init__(self, upstream: str, *, inject_method: str, inject_path: str) -> None:
        self.upstream = upstream.rstrip("/")
        self.inject_method = inject_method
        self.inject_path = inject_path
        self.seen: list[tuple[str, str]] = []
        self._injected = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Transient429ProxyHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def should_inject(self, method: str, path: str) -> bool:
        with self._lock:
            self.seen.append((method, path))
            if method == self.inject_method and path == self.inject_path and self._injected == 0:
                self._injected += 1
                return True
        return False

    def hits(self, method: str, path: str) -> int:
        with self._lock:
            return sum(1 for m, p in self.seen if m == method and p == path)


class _Transient429ProxyHandler(BaseHTTPRequestHandler):
    server: _Transient429Proxy
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _handle(self) -> None:
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._send(502, b"")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        if self.server.should_inject(self.command, self.path):
            self._send(429, _RESOURCE_EXHAUSTED_BODY, extra={"Retry-After": "1"})
            return
        hop = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in hop}
        try:
            upstream_resp = httpx.request(
                self.command,
                self.server.upstream + self.path,
                headers=headers,
                content=body,
                timeout=120.0,
                follow_redirects=False,
            )
        except Exception as exc:  # upstream unreachable -> surface as 502
            self._send(502, str(exc).encode())
            return
        skip = {"transfer-encoding", "connection", "content-length", "content-encoding"}
        extra = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in skip}
        self._send(upstream_resp.status_code, upstream_resp.content, extra=extra)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

    def _send(self, status: int, body: bytes, *, extra: dict[str, str] | None = None) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)


# ──────────────────────────────── real server ─────────────────────────────────


@contextlib.contextmanager
def _omnigent_server(tmp_path: Path) -> Iterator[str]:
    """Spawn a real ``omnigent server`` (accounts auth, sqlite) and yield its URL.

    Self-contained: no LLM credentials (the model never runs on the aborted
    startup path) and no runner tunnel-token allow-list (so the CLI's own runner
    would be accepted, matching the deployed posture).
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "server.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    server_log = tmp_path / "server.log"

    env = {k: v for k, v in os.environ.items() if k not in _STALE_ENV_VARS}
    env["OPENAI_API_KEY"] = "dummy-key-unused-on-aborted-startup"
    apply_server_env(env, _REPO_ROOT)
    env.pop("OMNIGENT_RUNNER_TUNNEL_TOKEN", None)

    log_handle = open(server_log, "w")  # noqa: SIM115 - lives for the Popen lifetime
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
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            if proc.poll() is not None:
                raise RuntimeError(
                    f"server exited early (code {proc.returncode}); "
                    f"log tail:\n{server_log.read_text()[-3000:]}"
                )
            time.sleep(0.25)
        else:
            raise RuntimeError(
                f"server didn't pass health within {_SERVER_HEALTH_TIMEOUT_S}s; "
                f"log tail:\n{server_log.read_text()[-3000:]}"
            )
        yield base_url
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()


def _omnigent_console_script() -> Path | None:
    candidate = Path(sys.executable).parent / "omnigent"
    return candidate if candidate.is_file() else None


def _stop_host_daemon(data_dir: Path) -> None:
    """Kill any host daemon spawned into an isolated ``OMNIGENT_DATA_DIR``.

    The CLI double-forks a detached host daemon that keeps retrying its control
    tunnel; without this it would linger past the test. The pidfile's first line
    is the daemon pid.
    """
    pid_file = data_dir / "host.pid"
    if not pid_file.exists():
        return
    with contextlib.suppress(ValueError, OSError, IndexError):
        pid = int(pid_file.read_text().strip().splitlines()[0])
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


def _run_cli_until_exit(args: list[str], *, env: dict[str, str], timeout: float) -> str:
    """Run a native CLI under a PTY until it exits; return decoded output.

    The native CLIs render a tmux-backed terminal and need a real TTY, so they
    are driven through a pseudo-terminal even though the failure here fires
    during startup (before any TUI).
    """
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execve(args[0], args, env)
        except OSError:
            os._exit(127)
    chunks: list[bytes] = []
    exited = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if data:
                chunks.append(data)
            else:  # EOF: child closed the slave, i.e. the CLI returned
                exited = True
                break
    with contextlib.suppress(OSError):
        os.close(fd)
    if not exited:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)
    output = b"".join(chunks).decode("utf-8", "replace")
    assert exited, f"CLI did not exit within {timeout}s (killed); output tail:\n{output[-2000:]}"
    return output


# ───────────────────────────────── facet 1 ────────────────────────────────────


@pytest.mark.skipif(
    shutil.which("tmux") is None or _omnigent_console_script() is None,
    reason="native codex CLI journey needs `tmux` and the `omnigent` console script",
)
def test_native_codex_startup_retries_transient_429_on_session_creation(tmp_path: Path) -> None:
    """Retry session creation through the real Codex CLI and a fault-injection proxy."""
    with _omnigent_server(tmp_path) as upstream:
        proxy = _Transient429Proxy(upstream, inject_method="POST", inject_path="/v1/sessions")
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()

        config_home = tmp_path / "config"
        data_dir = tmp_path / "data"
        config_home.mkdir()
        data_dir.mkdir()
        (config_home / "config.yaml").write_text("", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k not in _STALE_ENV_VARS}
        env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
        env["OMNIGENT_CONFIG_HOME"] = str(config_home)
        env["OMNIGENT_DATA_DIR"] = str(data_dir)
        env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
        env["OMNIGENT_SKIP_ONBOARD"] = "1"
        env["TERM"] = "xterm-256color"
        env["LINES"] = str(_PTY_ROWS)
        env["COLUMNS"] = str(_PTY_COLS)

        console = _omnigent_console_script()
        assert console is not None  # guarded by skipif
        args = [str(console), "codex", "--server", proxy.base_url, "-p", "hello"]
        try:
            output = _run_cli_until_exit(args, env=env, timeout=_CLI_TIMEOUT_S)
        finally:
            proxy.shutdown()
            _stop_host_daemon(data_dir)

    session_posts = proxy.hits("POST", "/v1/sessions")
    # The transient 429 must no longer abort startup ...
    assert "Codex session creation failed (429)" not in output, (
        "startup aborted on a transient 429 instead of retrying it. "
        f"POST /v1/sessions count={session_posts}; output tail:\n{output[-2000:]}"
    )
    # ... and it must have been retried (a re-POST of session creation).
    assert session_posts >= 2, (
        f"expected session creation to be retried after a transient 429 "
        f"(>= 2 POST /v1/sessions); saw {session_posts}. output tail:\n{output[-2000:]}"
    )


# ───────────────────────────────── facet 2 ────────────────────────────────────


def test_runner_bind_retries_transient_429() -> None:
    """Retry a transient 429 while binding the runner over HTTP."""
    session_id = "conv_abc123"
    inject_path = f"/v1/sessions/{session_id}"
    server = _Transient429Server(
        inject_method="PATCH",
        inject_path=inject_path,
        success_status=200,
        success_body=json.dumps({"session_id": session_id}).encode(),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def _drive() -> None:
        async with httpx.AsyncClient(base_url=server.base_url, timeout=10.0) as client:
            await bind_session_runner(client, session_id, "runner_xyz789")

    try:
        try:
            asyncio.run(_drive())
        except click.ClickException as exc:
            pytest.fail(
                f"runner binding aborted on a transient 429 instead of retrying it : {exc}"
            )
    finally:
        server.shutdown()

    patches = server.hits("PATCH", inject_path)
    assert patches >= 2, (
        f"expected the runner bind to be retried after a transient 429 (>= 2 PATCH); "
        f"saw {patches}."
    )


# ───────────────────────────────── facet 3 ────────────────────────────────────


def test_policy_evaluation_retries_transient_429() -> None:
    """Retry transient policy-evaluation throttling without failing closed."""
    session_id = "conv_policy1"
    inject_path = f"/v1/sessions/{session_id}/policies/evaluate"
    server = _Transient429Server(
        inject_method="POST",
        inject_path=inject_path,
        success_status=200,
        success_body=json.dumps({"action": "allow"}).encode(),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    eval_request = {
        "phase": "tool_call",
        "tool_name": "shell",
        "arguments": {"command": "echo hi"},
    }
    try:
        resp, error = post_evaluate_with_retry(
            url=f"{server.base_url}{inject_path}",
            headers={},
            eval_request=eval_request,
            read_timeout=10.0,
            hook_label="codex evaluate-policy hook",
        )
    finally:
        server.shutdown()

    assert error is None and resp is not None, (
        "policy evaluation failed closed on a transient 429 instead of retrying it "
        f": error={error!r}"
    )
    assert resp.status_code == 200
    posts = server.hits("POST", inject_path)
    assert posts >= 2, (
        f"expected policy evaluation to be retried after a transient 429 (>= 2 POST); saw {posts}."
    )
