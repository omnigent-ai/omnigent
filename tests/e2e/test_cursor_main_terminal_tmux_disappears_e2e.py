"""E2E regression: cursor:main must survive a cursor-agent exit without the generic
``tmux unavailable ... cursor:main`` cascade, which needs ``keep_alive_after_exit``.
Skips unless ``cursor-agent`` and ``tmux`` are on PATH."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent._wrapper_labels import (
    CURSOR_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.cursor_native.main import _materialize_cursor_agent_spec
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.helpers import POLL_INTERVAL_S

# Worktree root (this file lives at <worktree>/tests/e2e/). Used to build an
# absolute PYTHONPATH for the daemon so the runner it spawns -- whose cwd is
# the session workspace, not this worktree -- can still import omnigent.
_WORKTREE = Path(__file__).resolve().parents[2]

# The exact log signature the buggy vanish-then-generic-log path emits.
_TMUX_UNAVAILABLE_RE = re.compile(
    r"tmux unavailable after \d+ consecutive probes for terminal cursor:main"
)

# Skip the whole module unless the real Cursor terminal toolchain is present:
# the launch path shells out to cursor-agent inside a runner-owned tmux pane.
pytestmark = [
    pytest.mark.skipif(
        (_reason := cli_unavailable_reason("cursor-agent")) is not None,
        reason=f"cursor-native tmux-disappear e2e needs a runnable 'cursor-agent' CLI; {_reason}.",
    ),
    # tmux is gated on presence only: its version flag is ``-V`` (not the
    # generic ``--version`` cli_unavailable_reason probes with), so that probe
    # false-negatives on a perfectly usable tmux.
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="cursor-native terminal launch needs 'tmux' on PATH.",
    ),
]


def _scan_home_logs_for(home: Path, pattern: re.Pattern[str], *, session_id: str) -> str | None:
    """Find the signature in this session's runner logs, excluding earlier retries."""
    for log_path in home.rglob(f"runner-{session_id}-*.log"):
        try:
            text = log_path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if pattern.search(line):
                return line
    return None


def test_log_scan_ignores_previous_session(tmp_path: Path) -> None:
    """A failed earlier attempt must not contaminate the current session."""
    signature = "tmux unavailable after 3 consecutive probes for terminal cursor:main"
    (tmp_path / "runner-previous-20260916.log").write_text(signature)
    current = tmp_path / "runner-current-20260916.log"
    current.write_text("cursor terminal started")
    assert _scan_home_logs_for(tmp_path, _TMUX_UNAVAILABLE_RE, session_id="current") is None
    current.write_text(signature)
    assert _scan_home_logs_for(tmp_path, _TMUX_UNAVAILABLE_RE, session_id="current") == signature


class _CursorHost:
    """A spawned host daemon that can launch the real cursor-agent TUI."""

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        host_id: str,
        home: Path,
        daemon_log: Path,
    ) -> None:
        self.proc = proc
        self.host_id = host_id
        self.home = home
        self.daemon_log = daemon_log


def _seed_cursor_home(home: Path) -> str:
    """Seed *home* with a host config and return its host id (no Cursor login needed)."""
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-cursor-tmux-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    return host_id


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float = 45.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


def _cursor_terminal_metadata(client: httpx.Client, session_id: str) -> dict | None:
    """cursor:main terminal metadata (carries ``tmux_socket``/``tmux_target``), or ``None``."""
    resp = client.get(f"/v1/sessions/{session_id}/resources", timeout=30.0)
    if resp.status_code != 200:
        return None
    for item in resp.json().get("data", []):
        if item.get("type") == "terminal" and item.get("name") == "cursor:main":
            return item.get("metadata") or {}
    return None


def _terminal_resource_present(client: httpx.Client, session_id: str) -> bool:
    """Whether cursor:main is still exposed; it is removed on exit, so present->absent
    marks the disappearance."""
    return _cursor_terminal_metadata(client, session_id) is not None


def _pane_pid(socket: str, target: str) -> int | None:
    """Return the PID of the process running in the tmux pane, or ``None``."""
    try:
        probe = subprocess.run(
            ["tmux", "-S", socket, "display-message", "-p", "-t", target, "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    out = probe.stdout.strip()
    return int(out) if out.isdigit() else None


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* is a live process this test can signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _capture_pane(socket: str, target: str) -> str:
    """Capture the pane's visible content, for failure messages."""
    try:
        probe = subprocess.run(
            ["tmux", "-S", socket, "capture-pane", "-t", target, "-p", "-e"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<capture failed: {exc}>"
    return (probe.stdout.strip() or probe.stderr.strip() or "<empty pane>")[-1500:]


@pytest.fixture(scope="module")
def cursor_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_CursorHost]:
    """Spawn one host daemon that can launch the real cursor-agent TUI."""
    home = tmp_path_factory.mktemp("cursor-tmux-home")
    host_id = _seed_cursor_home(home)
    daemon_log = home / "host-daemon.log"
    # Pin HOME + config/data dirs so the daemon and its runner read the seeded
    # config and write their logs under this HOME for the signature scan.
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    # Absolute worktree roots: the spawned runner's cwd is the workspace, so a
    # relative PYTHONPATH entry would dangle into ModuleNotFoundError.
    _existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_WORKTREE),
            str(_WORKTREE / "sdks" / "python-client"),
            str(_WORKTREE / "sdks" / "ui"),
        ]
        + ([_existing] if _existing else [])
    )
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    try:
        _wait_for_host_online(http_client, host_id, timeout=45.0)
        yield _CursorHost(proc=proc, host_id=host_id, home=home, daemon_log=daemon_log)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _create_cursor_native_session(client: httpx.Client, host: _CursorHost, workspace: Path) -> str:
    """Create a cursor-native session on *host*; triggers ``_auto_create_cursor_terminal``
    (``-f`` avoids prompts)."""
    with tempfile.TemporaryDirectory() as _tmp:
        spec_yaml = _materialize_cursor_agent_spec(Path(_tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = spec_yaml.encode()
        info = tarfile.TarInfo("cursor-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {
        "host_id": host.host_id,
        "workspace": str(workspace),
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: CURSOR_NATIVE_WRAPPER_VALUE,
        },
        "terminal_launch_args": ["-f"],
    }
    create = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("cursor-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=60.0,
    )
    assert create.status_code in (200, 201), f"session create failed: {create.text}"
    return str(create.json()["session_id"])


# cursor-agent cold-start in a fresh tmux pane is an external-CLI dependency a
# loaded shard can miss once; retry rather than red the shard.
@pytest.mark.timeout(360, method="signal")
@pytest.mark.flaky(reruns=1, reruns_delay=5)
def test_cursor_main_terminal_survives_cursor_exit_without_tmux_unavailable(
    cursor_host: _CursorHost,
    http_client: httpx.Client,
) -> None:
    """Killing cursor-agent must not log the generic "tmux unavailable" cascade;
    keep_alive_after_exit persists the dead pane so the exit is reported deterministically."""
    host = cursor_host
    workspace = host.home / "ws"
    workspace.mkdir(exist_ok=True)
    session_id = _create_cursor_native_session(http_client, host, workspace)

    try:
        # 1) Wait for the real cursor-agent TUI to launch inside the
        #    runner-owned tmux server, so the idle watcher is live before we
        #    kill it. Resolve the pane process via the terminal's tmux socket.
        socket: str | None = None
        target = "main"
        cursor_pid: int | None = None
        deadline = time.monotonic() + 150.0
        while time.monotonic() < deadline:
            meta = _cursor_terminal_metadata(http_client, session_id)
            if meta is not None:
                socket = meta.get("tmux_socket")
                target = meta.get("tmux_target", "main")
                if socket:
                    pid = _pane_pid(socket, target)
                    if pid is not None and _pid_alive(pid):
                        cursor_pid = pid
                        break
            if host.proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited (rc={host.proc.returncode}) before cursor-agent "
                    f"launched; log tail:\n{host.daemon_log.read_text()[-2000:]}"
                )
            time.sleep(1.0)
        assert socket is not None and cursor_pid is not None, (
            "the launched 'cursor-agent' process never appeared for session "
            f"{session_id!r}; the runner did not register a live cursor:main pane.\n"
            f"daemon log tail:\n{host.daemon_log.read_text()[-2000:]}"
        )

        # 2) Give the idle watcher a couple of poll intervals to arm.
        time.sleep(2.5)

        # 3) Kill cursor-agent -- models the organic crash/exit the KPI counts.
        os.kill(cursor_pid, signal.SIGKILL)

        # 4) Scan the runner logs for the failure signature while confirming the
        #    terminal exit is handled. On the buggy build the signature fires
        #    within ~3 probe intervals (~3-5s); scan generously past that.
        signature_line: str | None = None
        terminal_gone = False
        scan_deadline = time.monotonic() + 25.0
        while time.monotonic() < scan_deadline:
            hit = _scan_home_logs_for(host.home, _TMUX_UNAVAILABLE_RE, session_id=session_id)
            if hit is not None:
                signature_line = hit
                break
            if not _terminal_resource_present(http_client, session_id):
                terminal_gone = True
            time.sleep(0.5)

        # Sanity: the kill actually exercised the terminal-exit path (the
        # terminal is removed on both the buggy and fixed builds), so a green
        # result reflects the fix, not a no-op where cursor-agent never died.
        if signature_line is None and not terminal_gone:
            terminal_gone = not _terminal_resource_present(http_client, session_id)
        assert terminal_gone or signature_line is not None, (
            "cursor:main terminal never exited after cursor-agent was killed -- the "
            "exit path was not exercised, so the reproduction is inconclusive.\n"
            f"final pane:\n{_capture_pane(socket, target)}"
        )

        # The regression assertion.
        assert signature_line is None, (
            "Killing cursor-agent vaporized the cursor:main "
            "tmux server (launched without keep_alive_after_exit), and the idle "
            "watcher logged the generic tmux-unavailable signature instead of a "
            f"diagnosable pane-dead exit:\n    {signature_line}"
        )
    finally:
        # Let the runner stop its watchers before tearing down its tmux server.
        with contextlib.suppress(httpx.HTTPError):
            http_client.delete(f"/v1/sessions/{session_id}", timeout=15.0)
