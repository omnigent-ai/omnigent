"""Remote connect to a Databricks-fronted server: auth must gate the host daemon.

With no usable credential, ``omnigent run <agent.yaml> --server <databricks-apps-url>
-p hi`` (stdin not a TTY) must fail fast with the ``omnigent login`` hint and leave
nothing behind: no host daemon record or log, and no tunnel dial.

A loopback stub plays the Databricks Apps edge: every request 302s to the workspace
OIDC authorize page (the signature the pre-flight keys on), the tunnel upgrade
included. A real ``omnigent run`` subprocess drives the whole journey. It is launched with
``python -c "from omnigent.cli import main; main()"`` so the live ``omnigent.cli``
module (the one the installed ``omnigent`` console script runs) executes; ``-m
omnigent.cli`` would re-execute the file under ``__main__`` and the child-side stub
below would not reach it. Only one collaborator is substituted, in the child via
``sitecustomize`` (see ``credential_stub``): ``_databricks_workspace_auth_info``
returns ``None``, exactly the result a credential-less user gets, so the real
``_ensure_backend`` ordering, daemon-spawn gating, and registry/log/tunnel
observation still run.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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

# Ambient credentials would defeat the staged never-logged-in state, and any
# runner/host wiring would push the child onto the zygote-fork path.
_ENV_PREFIXES_TO_CLEAR = ("DATABRICKS_", "OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_ENV_TO_CLEAR = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_DATABASE_URI",
    "RUNNER_SERVER_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


class _DatabricksAppsEdgeHandler(BaseHTTPRequestHandler):
    """Unauthenticated requests bounce to the workspace OIDC authorize page."""

    protocol_version = "HTTP/1.1"
    server: _DatabricksAppsEdge

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.record(self.command, self.path, bool(self.headers.get("Authorization")))
        if self.path.startswith("/.well-known/"):
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"redirecting to login"
            self.send_response(302)
            self.send_header("Location", self.server.authorize_url)
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


class _DatabricksAppsEdge(ThreadingHTTPServer):
    """Loopback stand-in for the Databricks Apps edge in front of an Omnigent server."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _DatabricksAppsEdgeHandler)
        self._lock = threading.Lock()
        self._requests: list[tuple[str, str, bool]] = []
        port = self.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.authorize_url = (
            f"https://127.0.0.1:{port}/oidc/oauth2/v2.0/authorize?response_type=code&scope=default"
        )

    def record(self, method: str, path: str, has_auth: bool) -> None:
        with self._lock:
            self._requests.append((method, path, has_auth))

    def tunnel_requests(self) -> list[tuple[str, str, bool]]:
        with self._lock:
            return [seen for seen in self._requests if _TUNNEL_PATH.match(seen[1])]


@pytest.fixture(scope="module")
def databricks_apps_edge() -> Iterator[_DatabricksAppsEdge]:
    """Run the stub edge; its ``url`` is e.g. ``"http://127.0.0.1:8471"``."""
    server = _DatabricksAppsEdge()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _recorded_daemons(data_dir: Path) -> list[tuple[int, float | None]]:
    registry = data_dir / "daemons"
    found: list[tuple[int, float | None]] = []
    for record in registry.glob("*.json") if registry.exists() else []:
        with contextlib.suppress(FileNotFoundError, ValueError):
            data = json.loads(record.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
                continue
            started = data.get("started_at")
            started_at = float(started) if isinstance(started, (int, float)) else None
            found.append((data["pid"], started_at))
    return found


def _is_recorded_daemon(proc: psutil.Process, started_at: float | None) -> bool:
    """Confirm the recorded PID is still our daemon before killing it.

    A daemon can exit and the OS reuse its PID; a stranger would have started
    after the record, so require creation at or before the recorded claim (small
    slack) and an omnigent command line.
    """
    try:
        if started_at is not None and proc.create_time() > started_at + 5:
            return False
        return any("omnigent" in part.lower() for part in proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _reap_recorded_daemons(data_dir: Path) -> None:
    """Kill every daemon the isolated registry still names, with its children."""
    procs: list[psutil.Process] = []
    for pid, started_at in _recorded_daemons(data_dir):
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue
        if not _is_recorded_daemon(proc, started_at):
            continue
        procs.extend(proc.children(recursive=True))
        procs.append(proc)
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
def credential_stub(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A ``sitecustomize`` dir forcing the child's workspace credential lookup to None.

    It also touches ``$OMNIGENT_TEST_STUB_MARKER`` so the launch can assert the stub
    actually loaded; without it the child would fall through to the real Databricks
    SDK, whose credential resolution makes a nondeterministic network attempt against
    the loopback stub.
    """
    stub = tmp_path_factory.mktemp("credential-stub")
    # Fail loudly if a cli.py rename drops the patch target: site swallows plain
    # exceptions from sitecustomize, so raise SystemExit to actually abort the child.
    (stub / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from omnigent import cli\n"
        "if not hasattr(cli, '_databricks_workspace_auth_info'):\n"
        "    raise SystemExit('stub target missing')\n"
        "cli._databricks_workspace_auth_info = lambda _host: None\n"
        "_marker = os.environ.get('OMNIGENT_TEST_STUB_MARKER')\n"
        "if _marker:\n"
        "    Path(_marker).write_text('1')\n"
    )
    return stub


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


def _launch_env(home: Path, credential_stub: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _ENV_TO_CLEAR and not key.startswith(_ENV_PREFIXES_TO_CLEAR)
    }
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    env["OMNIGENT_TEST_STUB_MARKER"] = str(home / ".omnigent" / "stub-ran")
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["TERM"] = "dumb"
    env["PYTHONPATH"] = os.pathsep.join(
        entry
        for entry in (
            str(credential_stub),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        )
        if entry  # a trailing empty entry would put the child's CWD on sys.path
    )
    return env


@dataclass
class _FailedLaunch:
    output: str
    daemon_records: list[str]
    host_logs: list[str]
    tunnel_requests: list[tuple[str, str, bool]]


@pytest.fixture(scope="module")
def failed_launch(
    databricks_apps_edge: _DatabricksAppsEdge,
    credential_less_home: Path,
    credential_stub: Path,
    agent_yaml: Path,
) -> _FailedLaunch:
    """Launch headless with no usable credential, then look for what it left."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "run",
            str(agent_yaml),
            "--server",
            databricks_apps_edge.url,
            "-p",
            "hi",
        ],
        env=_launch_env(credential_less_home, credential_stub),
        cwd=str(credential_less_home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 0 or "omnigent login" not in output:
        pytest.fail(f"launch did not fail with the sign-in hint:\n{output}")

    data_dir = credential_less_home / ".omnigent"
    if not (data_dir / "stub-ran").exists():
        pytest.fail(
            "the credential-less stub never loaded in the launched CLI; the run may "
            f"have reached the real Databricks SDK instead of failing fast:\n{output}"
        )

    def _records() -> list[str]:
        return sorted(p.name for p in (data_dir / "daemons").glob("*.json"))

    deadline = time.monotonic() + _DAEMON_RECORD_WINDOW_S
    records = _records()
    while not records and time.monotonic() < deadline:
        time.sleep(0.2)
        records = _records()

    # Only a spawned daemon dials the tunnel; with no record there is nothing to wait for.
    deadline = time.monotonic() + (_TUNNEL_DIAL_WINDOW_S if records else 0.0)
    while True:
        tunnel_requests = databricks_apps_edge.tunnel_requests()
        if tunnel_requests or time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    # Sample the host log after the full observation window so a daemon that writes
    # it slightly after its record or tunnel dial is still caught, keeping all three
    # orphan signals consistent.
    host_logs = sorted(p.name for p in (data_dir / "logs" / "host").glob("*.log"))

    return _FailedLaunch(
        output=output,
        daemon_records=records,
        host_logs=host_logs,
        tunnel_requests=tunnel_requests,
    )


@pytest.mark.timeout(_RUN_TIMEOUT_S + 60)
def test_failed_databricks_auth_leaves_no_host_daemon(failed_launch: _FailedLaunch) -> None:
    assert "Not signed in" in failed_launch.output, failed_launch.output
    assert failed_launch.daemon_records == [] and failed_launch.host_logs == [], (
        "the failed launch spawned a host daemon: "
        f"records={failed_launch.daemon_records} logs={failed_launch.host_logs}\n"
        f"--- CLI output ---\n{failed_launch.output}"
    )


@pytest.mark.timeout(_RUN_TIMEOUT_S + 60)
def test_failed_databricks_auth_never_dials_tunnel(failed_launch: _FailedLaunch) -> None:
    assert failed_launch.tunnel_requests == [], (
        "the host daemon dialed the server tunnel before authentication completed "
        f"(method, path, authorization sent): {failed_launch.tunnel_requests}"
    )
