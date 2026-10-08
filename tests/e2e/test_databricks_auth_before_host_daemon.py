"""Remote connect to a Databricks-fronted server: auth must gate the host daemon.

With no usable credential, ``omnigent run <agent.yaml> --server <databricks-apps-url>
-p hi`` (stdin not a TTY) must fail fast with the ``omnigent login`` hint and leave
nothing behind: no host daemon, no tunnel dial, and ``omnigent stop`` prints
``Nothing to stop.``.

A loopback stub plays the Databricks Apps edge (302 to the workspace OIDC authorize
page, also for the tunnel upgrade). A ``~/.databrickscfg`` profile pinned to the
stub carries a stale bearer, so the credential mint stays local and instant while
the server still rejects it: the launch fails on the same auth gate a
credential-less live run hits.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import psutil
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_RUN_TIMEOUT_S = 60
# The daemon claims its registry record within a second or two of being
# spawned; poll briefly so a buggy build's orphan is observed deterministically
# while a fixed build (which spawns none) still returns within the window.
_DAEMON_RECORD_WINDOW_S = 5.0
# It then dials the tunnel shortly after; this bounds how long a run that did
# spawn a daemon waits before concluding it never dialed.
_TUNNEL_DIAL_WINDOW_S = 8.0
_TUNNEL_PATH = re.compile(r"^/v1/hosts/[^/]+/tunnel$")

_ENV_TO_CLEAR = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CONFIG_FILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
    "OMNIGENT_HOST_NAME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


class _DatabricksAppsEdgeHandler(BaseHTTPRequestHandler):
    """Unauthenticated requests bounce to the workspace OIDC authorize page."""

    protocol_version = "HTTP/1.1"
    requests_seen: list[tuple[str, str, bool]] = []
    authorize_url = ""

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        has_auth = bool(self.headers.get("Authorization"))
        type(self).requests_seen.append((self.command, self.path, has_auth))
        if self.path.startswith("/.well-known/"):
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"redirecting to login"
            self.send_response(302)
            self.send_header("Location", type(self).authorize_url)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Server", "databricks")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer
    do_PATCH = _answer
    do_PUT = _answer
    do_DELETE = _answer

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def databricks_apps_edge() -> Iterator[str]:
    """Run the stub edge; yields its base URL, e.g. ``"http://127.0.0.1:8471"``."""
    _DatabricksAppsEdgeHandler.requests_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DatabricksAppsEdgeHandler)
    port = server.server_address[1]
    # The workspace must be https for the edge detector; point it at the stub's
    # own origin so the staged credential's host matches and no probe leaves
    # the loopback.
    _DatabricksAppsEdgeHandler.authorize_url = (
        f"https://127.0.0.1:{port}/oidc/oauth2/v2.0/authorize?response_type=code&scope=default"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _reap_recorded_daemons(data_dir: Path) -> None:
    """Kill every daemon the isolated registry still names, with its children."""
    registry = data_dir / "daemons"
    pids: set[int] = set()
    for record in registry.glob("*.json") if registry.exists() else []:
        match = re.search(r'"pid":\s*(\d+)', record.read_text())
        if match:
            pids.add(int(match.group(1)))
    procs: list[psutil.Process] = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            procs.extend(proc.children(recursive=True))
            procs.append(proc)
        except psutil.NoSuchProcess:
            continue
    gone = (psutil.NoSuchProcess, psutil.AccessDenied)
    for proc in procs:
        with contextlib.suppress(*gone):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=5)
    for proc in alive:
        with contextlib.suppress(*gone):
            proc.kill()


@pytest.fixture(scope="module")
def credential_less_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """An isolated ``$HOME`` with no usable Omnigent or Databricks credential."""
    home = tmp_path_factory.mktemp("home")
    (home / ".omnigent").mkdir()
    try:
        yield home
    finally:
        _reap_recorded_daemons(home / ".omnigent")


@pytest.fixture(scope="module")
def agent_yaml(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("agent") / "agent.yaml"
    path.write_text(
        "name: remote-connect-repro\n"
        "description: Minimal agent for the remote connect launch.\n"
        "executor:\n"
        "  model: gpt-4o\n"
        "prompt: |\n"
        "  You are a test agent.\n"
    )
    return path


def _launch_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    env["DATABRICKS_CONFIG_FILE"] = str(home / ".databrickscfg")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["TERM"] = "dumb"
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    return env


def _omnigent(args: list[str], *, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnigent.cli", *args],
        env=_launch_env(home),
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
    )


@dataclass
class _FailedLaunch:
    output: str
    daemon_records: list[str]
    host_logs: list[str]
    tunnel_requests: list[tuple[str, str, bool]] = field(default_factory=list)
    stop_output: str = ""


@pytest.fixture(scope="module")
def failed_launch(
    databricks_apps_edge: str, credential_less_home: Path, agent_yaml: Path
) -> _FailedLaunch:
    """Launch headless with no usable credential, then look for what it left."""
    # Stale bearer for the stub's workspace host: minted locally, rejected by the server.
    workspace = f"https://{urlparse(databricks_apps_edge).netloc}"
    (credential_less_home / ".databrickscfg").write_text(
        f"[stale]\nhost = {workspace}\ntoken = stale-access-token\n"
    )
    proc = _omnigent(
        ["run", str(agent_yaml), "--server", databricks_apps_edge, "-p", "hi"],
        home=credential_less_home,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 0 or "omnigent login" not in output:
        pytest.fail(f"launch did not fail with the sign-in hint:\n{output}")

    data_dir = credential_less_home / ".omnigent"

    def _records() -> list[str]:
        return sorted(p.name for p in (data_dir / "daemons").glob("*.json"))

    deadline = time.monotonic() + _DAEMON_RECORD_WINDOW_S
    records = _records()
    while not records and time.monotonic() < deadline:
        time.sleep(0.2)
        records = _records()
    host_logs = sorted(p.name for p in (data_dir / "logs" / "host").glob("*.log"))

    # Only a spawned daemon dials the tunnel; with no record there is nothing to wait for.
    deadline = time.monotonic() + (_TUNNEL_DIAL_WINDOW_S if records else 0.0)
    while True:
        tunnel_requests = [
            seen
            for seen in _DatabricksAppsEdgeHandler.requests_seen
            if _TUNNEL_PATH.match(seen[1])
        ]
        if tunnel_requests or time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    stop = _omnigent(["stop"], home=credential_less_home)
    return _FailedLaunch(
        output=output,
        daemon_records=records,
        host_logs=host_logs,
        tunnel_requests=tunnel_requests,
        stop_output=stop.stdout + stop.stderr,
    )


@pytest.mark.timeout(_RUN_TIMEOUT_S * 2 + 60)
def test_failed_databricks_auth_leaves_no_host_daemon(failed_launch: _FailedLaunch) -> None:
    assert "Not signed in" in failed_launch.output, failed_launch.output
    assert failed_launch.daemon_records == [] and failed_launch.host_logs == [], (
        "the failed launch spawned a host daemon: "
        f"records={failed_launch.daemon_records} logs={failed_launch.host_logs}\n"
        f"--- CLI output ---\n{failed_launch.output}"
    )
    assert "Nothing to stop." in failed_launch.stop_output, (
        f"`omnigent stop` found leftovers from the failed launch:\n{failed_launch.stop_output}"
    )


@pytest.mark.timeout(_RUN_TIMEOUT_S * 2 + 60)
def test_failed_databricks_auth_never_dials_tunnel(failed_launch: _FailedLaunch) -> None:
    assert failed_launch.tunnel_requests == [], (
        "the host daemon dialed the server tunnel before authentication completed "
        f"(method, path, authorization sent): {failed_launch.tunnel_requests}"
    )
