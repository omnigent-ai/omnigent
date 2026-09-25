"""E2e: a transient timeout on the OpenCode launch-config fetch must not
permanently lose the native terminal.

Reproduces the failing user journey: a host-bound ``opencode-native-ui`` session
is created while the runner->server HTTP path suffers ONE transient stall,
so the runner's launch-config fetch (``GET /v1/sessions/<id>``, 10s client
timeout in ``_opencode_native_launch_config``) raises ``httpx.ReadTimeout``.
The buggy build converts that single hiccup into ``RuntimeError("Could not
fetch OpenCode launch config for '<id>'.")``, which ``_launch_native_terminal``
logs as ``Failed to auto-create opencode terminal for <id>`` and publishes as
``native_terminal_start_failed`` -- the terminal is permanently lost even
though the server is healthy again a second later.

Regression contract asserted here: one transient timeout on that fetch must be
absorbed (bounded retry / recovery), so ``terminal_opencode_main`` still
registers. On the buggy build the terminal never appears and this test fails,
quoting the exact runner-log failure signature.

The rig points the host daemon's ``--server`` at a TCP-level reverse proxy in
front of the live e2e server. The proxy relays everything -- including the
host and runner WebSocket tunnels -- byte-for-byte, except that after the
runner's tunnel upgrade is seen it stalls exactly one bare
``GET /v1/sessions/<id>`` for longer than the 10s client timeout: a
transient network fault injected on the real journey.

Needs ``opencode`` and ``tmux`` on PATH (same prerequisites as the sibling
``test_host_opencode_native_e2e.py`` terminal auto-create test); no LLM turn
is driven, so no model credentials are required.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native.native_coding_agents import OPENCODE_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S


def _functional_opencode_dir() -> str | None:
    """Return the dir of the first PATH ``opencode`` that answers ``--version``.

    Some environments put a wrapper shim earlier on PATH that needs extra env
    (e.g. a gateway-config shim); scan every PATH entry and pick the first
    binary that actually works, so the daemon's readiness probe and the
    runner's terminal launch see a functional CLI.
    """
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(path_dir) / "opencode"
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        try:
            probe = subprocess.run([str(candidate), "--version"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return path_dir
    return None


_OPENCODE_BIN_DIR = _functional_opencode_dir()

pytestmark = pytest.mark.skipif(
    _OPENCODE_BIN_DIR is None or shutil.which("tmux") is None,
    reason="opencode-native terminal e2e needs a working `opencode` and `tmux` on PATH",
)

# The launch-config fetch in _opencode_native_launch_config uses timeout=10.0;
# stall a hair longer so exactly one httpx.ReadTimeout fires.
_STALL_SECONDS = 12.0

_RUNNER_TUNNEL_RE = re.compile(rb"^GET /v1/runners/[^ ]+/tunnel HTTP/1\.[01]\r\n")
# A *bare* session snapshot GET (no /resources, /events, ... suffix): the shape
# of the launch-config fetch. Query strings deliberately not matched.
_BARE_SESSION_GET_RE = re.compile(rb"^GET /v1/sessions/[0-9A-Za-z_.~%-]+ HTTP/1\.[01]\r\n")


class _CloseConnection(Exception):
    """Internal: tear down both sides of a proxied connection."""


class _TransientStallProxy:
    """TCP reverse proxy that injects one transient stall on the runner path.

    Relays all connections between clients (the host daemon and the runners it
    spawns) and the live e2e server. Request heads on the client->server
    stream are parsed just enough to (a) notice the runner's WebSocket tunnel
    upgrade (``GET /v1/runners/<id>/tunnel``) and (b) after that upgrade has
    been seen, hold back exactly one bare ``GET /v1/sessions/<id>`` -- the
    runner's OpenCode launch-config fetch -- for :data:`_STALL_SECONDS`, then
    sever that connection. Every other byte (WebSocket frames, SSE, POST
    bodies) flows through untouched, so the journey is real except for the one
    transient fault.
    """

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._t0 = time.monotonic()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port: int = self._listener.getsockname()[1]
        self.runner_tunnel_seen = threading.Event()
        self.stall_fired = threading.Event()
        self.stalled_request_line: str | None = None
        self.request_log: list[tuple[float, str]] = []
        self._log_lock = threading.Lock()
        self._stall_lock = threading.Lock()
        self._stall_claimed = False
        self._closing = False
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._closing = True
        with contextlib.suppress(OSError):
            self._listener.close()

    def dump_requests(self) -> str:
        with self._log_lock:
            return "\n".join(f"  t+{t:7.2f}s  {line}" for t, line in self.request_log)

    def _log_request(self, line: bytes) -> None:
        with self._log_lock:
            if len(self.request_log) < 500:
                self.request_log.append(
                    (time.monotonic() - self._t0, line.decode("latin-1", "replace"))
                )

    def _claim_stall(self) -> bool:
        with self._stall_lock:
            if self._stall_claimed:
                return False
            self._stall_claimed = True
            return True

    def _accept_loop(self) -> None:
        while not self._closing:
            try:
                client, _addr = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self._upstream, timeout=10.0)
        except OSError:
            client.close()
            return
        client.settimeout(None)
        upstream.settimeout(None)
        down = threading.Thread(target=self._pump_blind, args=(upstream, client), daemon=True)
        down.start()
        try:
            self._pump_client_to_server(client, upstream)
        except (_CloseConnection, OSError):
            pass
        finally:
            for sock in (client, upstream):
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _pump_blind(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock, how in ((dst, socket.SHUT_WR), (src, socket.SHUT_RD)):
                with contextlib.suppress(OSError):
                    sock.shutdown(how)

    def _pump_client_to_server(self, client: socket.socket, upstream: socket.socket) -> None:
        """Relay client->server, parsing request heads until the stream goes opaque.

        A tiny HTTP/1.1 state machine: ``head`` accumulates until a full
        request head is buffered, ``body`` counts down a Content-Length body,
        and ``blind`` (after a WebSocket upgrade or chunked body) forwards
        bytes without parsing for the connection's remaining life.
        """
        buf = b""
        mode = "head"  # "head" | "body" | "blind"
        body_remaining = 0
        while True:
            made_progress = True
            while made_progress and buf:
                made_progress = False
                if mode == "blind":
                    upstream.sendall(buf)
                    buf = b""
                elif mode == "body":
                    chunk = buf[:body_remaining]
                    upstream.sendall(chunk)
                    buf = buf[len(chunk) :]
                    body_remaining -= len(chunk)
                    if body_remaining == 0:
                        mode = "head"
                        made_progress = True
                else:
                    head_end = buf.find(b"\r\n\r\n")
                    if head_end < 0:
                        break
                    head = buf[: head_end + 4]
                    request_line = head.split(b"\r\n", 1)[0]
                    self._log_request(request_line)
                    if _RUNNER_TUNNEL_RE.match(head):
                        self.runner_tunnel_seen.set()
                    if (
                        self.runner_tunnel_seen.is_set()
                        and not self.stall_fired.is_set()
                        and _BARE_SESSION_GET_RE.match(head)
                        and self._claim_stall()
                    ):
                        self.stalled_request_line = request_line.decode("latin-1", "replace")
                        self.stall_fired.set()
                        # Hold the request past the client's 10s read timeout,
                        # then sever the connection: one transient network
                        # fault, over by the time anything retries.
                        time.sleep(_STALL_SECONDS)
                        raise _CloseConnection
                    lowered = head.lower()
                    if b"\r\nupgrade:" in lowered:
                        # WebSocket tunnel (host or runner): forward and go
                        # opaque for the connection's life.
                        upstream.sendall(buf)
                        buf = b""
                        mode = "blind"
                        made_progress = True
                        continue
                    upstream.sendall(head)
                    buf = buf[head_end + 4 :]
                    if b"\r\ntransfer-encoding: chunked" in lowered:
                        # Chunked request body: no cheap end-of-message marker;
                        # go opaque rather than mis-frame later requests.
                        mode = "blind"
                    else:
                        match = re.search(rb"\r\ncontent-length:[ \t]*(\d+)", lowered)
                        body_remaining = int(match.group(1)) if match else 0
                        mode = "body" if body_remaining else "head"
                    made_progress = True
            data = client.recv(65536)
            if not data:
                with contextlib.suppress(OSError):
                    upstream.shutdown(socket.SHUT_WR)
                return
            buf += data


def _spawn_host_daemon(
    *, tmp_path: Path, server_url: str, data_dir: Path
) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent host`` daemon pointed at *server_url* (the proxy)."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    # Absolute entries only: the runner's cwd is the session workspace, so a
    # relative sdks/python-client entry (omnigent_client) would stop resolving.
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(repo_root),
            str(repo_root / "sdks" / "python-client"),
            str(repo_root / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    # Pin the data dir so the runner's process log (where the bug's
    # "Failed to auto-create opencode terminal" signature lands) is
    # discoverable under <data_dir>/logs/runner/.
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    # The daemon gates opencode-native launches on an available provider
    # credential (opencode_auth_summary().has_provider). Terminal auto-create
    # never calls the LLM, so a placeholder env key is enough to un-gate the
    # launch without granting anything.
    env.setdefault("OPENAI_API_KEY", "sk-e2e-placeholder")
    # Put the functional opencode first: readiness gating and the runner's
    # `opencode serve` launch must not hit a broken wrapper shim.
    if _OPENCODE_BIN_DIR:
        env["PATH"] = os.pathsep.join([_OPENCODE_BIN_DIR, env.get("PATH", "")])
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", server_url],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _online_host_id(client: httpx.Client, timeout: float = 60.0) -> str:
    """Poll ``GET /v1/hosts`` until at least one host is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _terminal_registered(client: httpx.Client, *, session_id: str, resource_id: str) -> bool:
    """Return whether the terminal resource is registered for the session."""
    resp = client.get(f"/v1/sessions/{session_id}/resources")
    if resp.status_code != 200:
        return False
    data = resp.json().get("data", [])
    return any(r.get("id") == resource_id and r.get("type") == "terminal" for r in data)


def _runner_log_excerpt(data_dir: Path, *needles: str) -> str:
    """Return the matching lines (with 1 line of context) from runner logs."""
    lines: list[str] = []
    for log in sorted((data_dir / "logs" / "runner").glob("*.log")):
        try:
            content = log.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(content):
            if any(n in line for n in needles):
                lines.extend(content[max(0, i - 1) : i + 2])
    return "\n".join(lines[-60:])


@pytest.mark.timeout(420)
def test_transient_launch_config_timeout_does_not_lose_terminal(
    http_client: httpx.Client,
    tmp_path: Path,
    live_server: str,
) -> None:
    """One transient ReadTimeout on the launch-config fetch must be absorbed.

    Journey: create a host-bound opencode-native session -> the
    runner begins terminal auto-create and fetches the launch config
    (``GET /v1/sessions/<id>``) -> that one request hits a transient network
    stall and times out -> the terminal must STILL come up once the network
    recovers. On the buggy build a single timeout permanently fails the
    terminal (``native_terminal_start_failed``; runner log: ``Failed to
    auto-create opencode terminal for <id>`` / ``Could not fetch OpenCode
    launch config``) and this assertion fails.
    """
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None, "opencode-native-ui agent not seeded"

    workspace = tmp_path / "ws"
    workspace.mkdir()
    data_dir = tmp_path / "omnigent-data"
    data_dir.mkdir()

    server_host, server_port = live_server.split("//", 1)[1].rsplit(":", 1)
    proxy = _TransientStallProxy(server_host, int(server_port))
    daemon = _spawn_host_daemon(tmp_path=tmp_path, server_url=proxy.url, data_dir=data_dir)
    try:
        host_id = _online_host_id(http_client)
        create = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        # Rig checks (not the bug): the runner must come up through the proxy
        # and the transient fault must actually fire on a bare session GET.
        assert proxy.runner_tunnel_seen.wait(timeout=90.0), (
            "rig failure: the runner never opened its WebSocket tunnel through "
            f"the proxy.\nProxied requests:\n{proxy.dump_requests()}"
        )
        assert proxy.stall_fired.wait(timeout=90.0), (
            "rig failure: no bare GET /v1/sessions/<id> arrived from the runner "
            "after its tunnel upgrade, so the transient fault was never "
            f"injected.\nProxied requests:\n{proxy.dump_requests()}"
        )

        # The bug's contract: the single transient timeout must be absorbed --
        # the opencode terminal still registers once the network is healthy.
        terminal_id = terminal_resource_id("opencode", "main")
        deadline = time.monotonic() + 150.0
        while time.monotonic() < deadline:
            if _terminal_registered(http_client, session_id=session_id, resource_id=terminal_id):
                break
            time.sleep(1.0)
        else:
            snapshot = http_client.get(f"/v1/sessions/{session_id}")
            log_excerpt = _runner_log_excerpt(
                data_dir,
                "Failed to auto-create opencode terminal",
                "Could not fetch OpenCode launch config",
                "ReadTimeout",
            )
            pytest.fail(
                "One transient timeout on the OpenCode launch-config "
                f"fetch permanently lost the native terminal: {terminal_id} never "
                f"registered for session {session_id} within 150s of the injected "
                f"stall (stalled request: {proxy.stalled_request_line}).\n"
                f"Session snapshot: HTTP {snapshot.status_code} "
                f"{snapshot.text[:600]}\n"
                f"Runner log evidence:\n{log_excerpt}\n"
                f"Proxied requests:\n{proxy.dump_requests()}"
            )
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        proxy.close()
