"""End-to-end: ``omnigent start`` when the host registers after the 30s grace.

``omnigent start`` (what deployment wrappers such as ``isaac omni`` run) spawns
the host daemon and waits up to 30s for the server to report it online. A host
that registers later — a slow startup step, a slow tunnel handshake — must
still end up online: ``start`` leaves the daemon running and reports it as
still connecting instead of killing it.

The slow registration is injected at the transport: a loopback proxy in front
of a real ``omnigent server`` forwards plain HTTP (the CLI's host-status
probes) at once but holds the host tunnel's WebSocket handshake until
``TUNNEL_STALL_S`` after the first attempt; the daemon retries and registers
once the window closes.

Run with::

    python -m pytest tests/e2e/test_start_slow_host_registration_e2e.py -v

Set ``OMNIGENT_REPRO_SERVER_URL`` to reuse an already-running server.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="daemon liveness checks rely on POSIX signals"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Hold the tunnel handshake past the CLI's 30s registration grace, with margin
# for the daemon's own startup before its first attempt.
TUNNEL_STALL_S = 40.0
# The host registers a few seconds after the stall window; the server must
# report it online well inside this budget however `start` handled the wait.
_ONLINE_DEADLINE_S = 90.0
_START_DEADLINE_S = 180.0
_SERVER_HEALTH_TIMEOUT_S = 90.0


class StallingTunnelProxy:
    """Loopback proxy that delays only WebSocket handshakes to the upstream server.

    Plain HTTP requests are forwarded at once. WebSocket upgrade requests are
    held unanswered until ``stall_s`` after the first one arrives, then
    forwarded, so a host behind the proxy registers late but does register.
    """

    def __init__(self, upstream_url: str, *, stall_s: float, port: int = 0) -> None:
        parts = urlsplit(upstream_url)
        if parts.scheme != "http":
            raise ValueError(f"StallingTunnelProxy forwards plain http only, got {upstream_url!r}")
        self._upstream = (parts.hostname or "127.0.0.1", parts.port or 80)
        self.stall_s = stall_s
        self.upgrade_attempts: list[float] = []
        self._first_upgrade_at: float | None = None
        self._lock = threading.Lock()
        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="stalling-tunnel-proxy", daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # ``shutdown`` would block forever if ``serve_forever`` never started.
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(10.0)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk:
                    return
                head += chunk
        except OSError:
            return
        headers = head.split(b"\r\n\r\n", 1)[0].lower()
        if b"upgrade: websocket" in headers and not self._stall(client):
            return
        try:
            upstream = socket.create_connection(self._upstream, timeout=10.0)
        except OSError:
            client.close()
            return
        with upstream, client:
            try:
                upstream.sendall(head)
            except OSError:
                return
            _pipe(client, upstream)

    def _stall(self, client: socket.socket) -> bool:
        """Hold *client* until the stall window ends; ``False`` if it hung up first."""
        now = time.monotonic()
        with self._lock:
            if self._first_upgrade_at is None:
                self._first_upgrade_at = now
            self.upgrade_attempts.append(now - self._first_upgrade_at)
            release_at = self._first_upgrade_at + self.stall_s
        client.setblocking(False)
        try:
            while time.monotonic() < release_at:
                readable, _, _ = select.select([client], [], [], 0.2)
                if not readable:
                    continue
                try:
                    if client.recv(1, socket.MSG_PEEK) == b"":
                        return False
                    # Peeked bytes stay buffered; do not spin on them.
                    time.sleep(0.2)
                except BlockingIOError:
                    continue
                except OSError:
                    return False
        finally:
            client.setblocking(True)
        return True


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until either side closes or errors."""
    a.settimeout(None)
    b.settimeout(None)
    while True:
        readable, _, _ = select.select([a, b], [], [], 1.0)
        for sock in readable:
            try:
                data = sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                (b if sock is a else a).sendall(data)
            except OSError:
                return


def _free_localhost_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _subprocess_env(**overrides: str) -> dict[str, str]:
    env = {**os.environ, **overrides}
    # Absolute SDK paths keep the in-repo workspace packages importable from
    # the child's cwd (an isolated home, not the repo root).
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    ).rstrip(os.pathsep)
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(proxy_var, None)
    return env


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.fixture(scope="module")
def upstream_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real ``omnigent server``; ``OMNIGENT_REPRO_SERVER_URL`` reuses a running one."""
    reuse = os.environ.get("OMNIGENT_REPRO_SERVER_URL")
    if reuse:
        yield reuse.rstrip("/")
        return
    base = tmp_path_factory.mktemp("slow-registration-server")
    port = _free_localhost_port()
    url = f"http://127.0.0.1:{port}"
    log_path = base / "server.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "server",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{base / 'server.db'}",
                "--artifact-location",
                str(base / "artifacts"),
                "--no-open",
            ],
            env=_subprocess_env(OMNIGENT_DATA_DIR=str(base / "data")),
            cwd=str(base),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                pytest.fail(
                    f"`omnigent server` exited early ({proc.returncode}):\n"
                    f"{log_path.read_text(errors='replace')[-4000:]}"
                )
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail(
                    f"`omnigent server` not healthy within {_SERVER_HEALTH_TIMEOUT_S:.0f}s:\n"
                    f"{log_path.read_text(errors='replace')[-4000:]}"
                )
            time.sleep(0.5)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@dataclass
class _CliRun:
    lines: list[tuple[float, str]]
    returncode: int | None
    elapsed_s: float
    daemon_pids: list[int]

    @property
    def output(self) -> str:
        return "\n".join(f"[{elapsed:6.1f}s] {text}" for elapsed, text in self.lines)


@dataclass
class _CliState:
    """Isolated HOME / config / data for one ``omnigent`` CLI journey."""

    env: dict[str, str]
    home: Path
    config_home: Path
    data_dir: Path

    @classmethod
    def create(cls, base: Path, server_url: str) -> _CliState:
        home, config_home, data_dir = base / "home", base / "config", base / "data"
        for directory in (home, config_home, data_dir):
            directory.mkdir()
        (config_home / "config.yaml").write_text(f"server: {server_url}\n")
        env = _subprocess_env(
            HOME=str(home),
            OMNIGENT_CONFIG_HOME=str(config_home),
            OMNIGENT_DATA_DIR=str(data_dir),
            OMNIGENT_HOST_NO_OPEN="1",
            OMNIGENT_SKIP_ONBOARD="1",
        )
        return cls(env=env, home=home, config_home=config_home, data_dir=data_dir)

    def host_id(self) -> str | None:
        config = yaml.safe_load((self.config_home / "config.yaml").read_text()) or {}
        return (config.get("host") or {}).get("host_id")

    def daemon_pids(self) -> list[int]:
        pids: list[int] = []
        for path in glob.glob(str(self.data_dir / "daemons" / "*.json")):
            try:
                pids.append(int(json.loads(Path(path).read_text())["pid"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return pids

    def run(self, *args: str, timeout: float) -> _CliRun:
        """Run ``omnigent <args>``, timestamping output and noting spawned daemon pids."""
        argv = [sys.executable, "-m", "omnigent", *args]
        start = time.monotonic()
        proc = subprocess.Popen(
            argv,
            env=self.env,
            cwd=str(self.home),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines: list[tuple[float, str]] = []
        daemon_pids: set[int] = set()
        done = threading.Event()

        def _read() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append((time.monotonic() - start, line.rstrip("\n")))

        def _sample_daemons() -> None:
            while not done.is_set():
                daemon_pids.update(self.daemon_pids())
                done.wait(0.25)

        reader = threading.Thread(target=_read, daemon=True)
        sampler = threading.Thread(target=_sample_daemons, daemon=True)
        reader.start()
        sampler.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        finally:
            done.set()
            reader.join(timeout=10)
            sampler.join(timeout=10)
        return _CliRun(
            lines=lines,
            returncode=proc.returncode,
            elapsed_s=time.monotonic() - start,
            daemon_pids=sorted(daemon_pids),
        )


def _host_status(server_url: str, host_id: str) -> str:
    try:
        resp = httpx.get(f"{server_url}/v1/hosts/{host_id}", timeout=5)
    except httpx.HTTPError as exc:
        return f"transport error: {exc}"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}"
    return str(resp.json().get("status"))


def _wait_host_online(server_url: str, host_id: str, *, deadline: float) -> float | None:
    """Return seconds until the server reports *host_id* online, or ``None`` at *deadline*.

    Probes at least once, so a deadline that has already passed still observes
    a host that is online.
    """
    started = time.monotonic()
    while True:
        if _host_status(server_url, host_id) == "online":
            return time.monotonic() - started
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def test_start_brings_late_registering_host_online(
    upstream_server_url: str, tmp_path: Path
) -> None:
    """A host whose registration outlasts the 30s grace must still end up online.

    ``start`` leaves the daemon it spawned running and reports the pending
    registration; the host registers once its slow step completes.
    """
    proxy = StallingTunnelProxy(upstream_server_url, stall_s=TUNNEL_STALL_S)
    state: _CliState | None = None
    spawned_pids: list[int] = []
    try:
        proxy.start()
        state = _CliState.create(tmp_path, proxy.url)
        launched = time.monotonic()
        run = state.run("start", "--non-interactive", timeout=_START_DEADLINE_S)
        spawned_pids = run.daemon_pids
        host_id = state.host_id()
        assert host_id, (
            f"`omnigent start` left no host identity in config.yaml; output:\n{run.output}"
        )
        assert run.returncode == 0, (
            f"`omnigent start` exited {run.returncode} instead of leaving the daemon "
            f"connecting; output:\n{run.output}"
        )
        assert "still connecting" in run.output, run.output
        assert "Started the host daemon" not in run.output, run.output
        assert run.daemon_pids and all(_pid_alive(pid) for pid in run.daemon_pids), (
            f"`omnigent start` did not leave its daemon running (spawned {run.daemon_pids}); "
            f"output:\n{run.output}"
        )
        online_after = _wait_host_online(
            upstream_server_url, host_id, deadline=time.monotonic() + _ONLINE_DEADLINE_S
        )
        daemons = {pid: ("alive" if _pid_alive(pid) else "gone") for pid in run.daemon_pids}
        assert online_after is not None, (
            f"host {host_id} never came online within {_ONLINE_DEADLINE_S:.0f}s after "
            f"`omnigent start` returned ({time.monotonic() - launched:.0f}s after launch), "
            f"although its tunnel handshake was only held for {TUNNEL_STALL_S:.0f}s "
            f"(handshake attempts at {[round(t, 1) for t in proxy.upgrade_attempts]}s). "
            f"`start` exited {run.returncode} after {run.elapsed_s:.1f}s; spawned daemon(s): "
            f"{daemons}; server now reports: {_host_status(upstream_server_url, host_id)}.\n"
            f"`omnigent start` output:\n{run.output}"
        )
    finally:
        if state is not None:
            state.run("stop", "--force", timeout=120)
        # `stop` only knows daemons still in its registry; never leak the rest.
        for pid in spawned_pids:
            if _pid_alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        proxy.stop()
