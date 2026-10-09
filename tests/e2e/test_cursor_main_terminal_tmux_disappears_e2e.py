"""E2E regression test: the cursor:main terminal must not disappear from tmux.

Guards against this runner-log signature (``omnigent.inner.terminal`` /
``_idle_watch_loop_threaded``)::

    tmux unavailable after 3 consecutive probes for terminal cursor:main

The Cursor ``main`` terminal must be launched with ``keep_alive_after_exit``:
without it, tmux's defaults (``exit-empty on`` + ``remain-on-exit off``) reap
the lone-pane server the instant ``cursor-agent`` exits, the idle watcher's
probes fail, and only the generic line above is logged, with no pane exit
status. Cursor sibling of ``test_pi_main_terminal_tmux_disappears_e2e``; reuses
its helpers.

The journey is real end-to-end: a host daemon comes online, a cursor-native
session makes its runner launch the real ``cursor-agent`` TUI in a runner-owned
tmux pane, then that process is killed. No Cursor login is needed: an
unauthenticated cursor-agent stays on its "Press any key to log in..." screen
until it is killed.

    .venv/bin/python -m pytest tests/e2e/test_cursor_main_terminal_tmux_disappears_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import psutil
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.native_session import create_native_session
from tests.e2e.helpers import POLL_INTERVAL_S
from tests.e2e.test_pi_main_terminal_tmux_disappears_e2e import (
    _scan_home_logs_for,
    _terminal_resource_present,
    _wait_for_host_online,
)

_WORKTREE = Path(__file__).resolve().parents[2]

_TMUX_UNAVAILABLE_RE = re.compile(
    r"tmux unavailable after \d+ consecutive probes for terminal cursor:main"
)

# The runner emits this once it diagnoses the pane-dead exit. Its presence
# distinguishes a handled exit from a terminal removed by some other path.
_TERMINAL_EXIT_OBSERVED_RE = re.compile(r"Terminal exit observed:.*terminal=cursor:main")

pytestmark = [
    pytest.mark.skipif(
        shutil.which("cursor-agent") is None,
        reason="cursor-native tmux-disappear e2e needs the 'cursor-agent' CLI on PATH.",
    ),
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="cursor-native terminal launch needs 'tmux' on PATH.",
    ),
]


class _CursorHost:
    """A spawned host daemon whose HOME carries no Cursor login.

    :param proc: The daemon subprocess handle.
    :param host_id: The registered host id.
    :param home: The daemon's HOME (holds ``.omnigent`` + the runner logs).
    :param daemon_log: Captured daemon log path.
    """

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


def _seed_host_home(home: Path) -> str:
    """Seed *home* with a host config only; the TUI then idles on its sign-in screen."""
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


@pytest.fixture(scope="module")
def cursor_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_CursorHost]:
    """Spawn one host daemon with an isolated, login-less HOME.

    :param live_server: Server URL the daemon registers with.
    :param http_client: HTTP client pointed at the server.
    :param tmp_path_factory: Module-scoped temp dir factory (the daemon HOME).
    :yields: The spawned :class:`_CursorHost`.
    """
    home = tmp_path_factory.mktemp("cursor-tmux-home")
    host_id = _seed_host_home(home)
    daemon_log = home / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    env.pop("CURSOR_API_KEY", None)
    # The runner the daemon spawns runs with cwd=<workspace>, so only absolute
    # PYTHONPATH entries keep ``omnigent`` importable there.
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


def _cursor_terminal_socket(client: httpx.Client, session_id: str) -> tuple[str, str] | None:
    """Return ``(tmux_socket, tmux_target)`` advertised by the session's cursor:main resource."""
    resp = client.get(f"/v1/sessions/{session_id}/resources", timeout=30.0)
    if resp.status_code != 200:
        return None
    for item in resp.json().get("data", []):
        metadata = item.get("metadata") or {}
        if item.get("type") != "terminal" or metadata.get("terminal_name") != "cursor":
            continue
        socket_path = metadata.get("tmux_socket")
        if isinstance(socket_path, str) and socket_path:
            return socket_path, str(metadata.get("tmux_target") or "main")
    return None


def _tmux(socket_path: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", socket_path, *args], capture_output=True, text=True, timeout=10.0
    )


def _pane_text(socket_path: str, target: str) -> str:
    probe = _tmux(socket_path, "capture-pane", "-p", "-t", target)
    if probe.returncode != 0:
        return f"<capture-pane failed rc={probe.returncode}: {probe.stderr.strip()}>"
    return probe.stdout


def _cursor_agent_pid(socket_path: str, target: str) -> int | None:
    """Find the live cursor-agent process in the pane (the pane process or a descendant)."""
    probe = _tmux(socket_path, "list-panes", "-t", target, "-F", "#{pane_pid}")
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    try:
        pane = psutil.Process(int(probe.stdout.split()[0]))
        candidates = [pane, *pane.children(recursive=True)]
    except (psutil.Error, ValueError):
        return None
    for proc in reversed(candidates):
        try:
            tokens = [proc.name(), *proc.cmdline()]
        except psutil.Error:
            continue
        if any("cursor-agent" in token or "/cursor/" in token for token in tokens):
            return proc.pid
    return pane.pid if pane.is_running() else None


@pytest.mark.timeout(360, method="signal")
def test_cursor_main_terminal_survives_cursor_agent_exit_without_tmux_unavailable(
    cursor_host: _CursorHost,
    live_server: str,
    http_client: httpx.Client,
) -> None:
    """Killing cursor-agent must not vaporize cursor:main or log "tmux unavailable".

    Drives the real cursor-native journey: create the session on the host, let
    the runner auto-launch the real ``cursor-agent`` TUI inside a runner-owned
    tmux server, then kill that process. The dead pane must persist so the idle
    watcher reports the exit without the generic ``tmux unavailable after N
    consecutive probes for terminal cursor:main`` line.

    :param cursor_host: The spawned host daemon.
    :param live_server: Server base URL.
    :param http_client: HTTP client pointed at the server.
    """
    host = cursor_host
    workspace = host.home / "ws"
    workspace.mkdir(exist_ok=True)
    created = create_native_session(
        http_client,
        live_server,
        harness="cursor",
        metadata={
            "host_id": host.host_id,
            "workspace": str(workspace),
            "terminal_launch_args": ["-f"],
        },
    )
    session_id = str(created["session_id"])

    try:
        # The TUI must have painted so the idle watcher is live before the kill.
        socket_info: tuple[str, str] | None = None
        pane_text = ""
        deadline = time.monotonic() + 150.0
        while time.monotonic() < deadline:
            socket_info = socket_info or _cursor_terminal_socket(http_client, session_id)
            if socket_info is not None:
                pane_text = _pane_text(*socket_info)
                if "Cursor Agent" in pane_text or "cursor" in pane_text.lower():
                    break
            if host.proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited (rc={host.proc.returncode}) before cursor-agent "
                    f"launched; log tail:\n{host.daemon_log.read_text()[-2000:]}"
                )
            time.sleep(1.0)
        assert socket_info is not None, (
            f"cursor:main terminal resource never appeared for session {session_id!r}; "
            f"daemon log tail:\n{host.daemon_log.read_text()[-2000:]}"
        )
        socket_path, target = socket_info
        assert "Cursor Agent" in pane_text or "cursor" in pane_text.lower(), (
            f"the cursor-agent TUI never painted in pane {target!r} on {socket_path}:\n{pane_text}"
        )
        time.sleep(2.5)

        pid = _cursor_agent_pid(socket_path, target)
        assert pid is not None, f"no live cursor-agent process found in pane {target!r}"
        os.kill(pid, signal.SIGKILL)

        # Generous window so a slow box cannot mask the signature; the
        # exit-observation line is collected alongside it so a green reflects
        # a handled pane-dead exit, not an unrelated resource removal.
        signature_line: str | None = None
        exit_observed_line: str | None = None
        terminal_gone = False
        scan_deadline = time.monotonic() + 25.0
        while time.monotonic() < scan_deadline:
            hit = _scan_home_logs_for(host.home, _TMUX_UNAVAILABLE_RE, session_id=session_id)
            if hit is not None:
                signature_line = hit
                break
            if exit_observed_line is None:
                exit_observed_line = _scan_home_logs_for(
                    host.home, _TERMINAL_EXIT_OBSERVED_RE, session_id=session_id
                )
            if not _terminal_resource_present(http_client, session_id):
                terminal_gone = True
            if exit_observed_line is not None and terminal_gone:
                break
            time.sleep(POLL_INTERVAL_S)
        if signature_line is None and not terminal_gone:
            terminal_gone = not _terminal_resource_present(http_client, session_id)
        assert terminal_gone or signature_line is not None, (
            "cursor:main never exited after cursor-agent was killed -- the exit path was "
            "not exercised, so the reproduction is inconclusive."
        )

        has_session = _tmux(socket_path, "has-session", "-t", target)
        assert signature_line is None, (
            "killing cursor-agent vaporized the cursor:main tmux server (keep_alive_after_exit "
            "not in effect), and the idle watcher logged the generic tmux-unavailable "
            "signature instead of a diagnosable pane-dead exit:\n"
            f"    {signature_line}\n"
            f"tmux has-session rc={has_session.returncode}: {has_session.stderr.strip()}"
        )
        assert exit_observed_line is not None, (
            "cursor:main was removed without a diagnosable terminal-exit record -- a green "
            "result must reflect the handled pane-dead exit, not an unrelated resource "
            f"removal.\n    tmux has-session rc={has_session.returncode}: "
            f"{has_session.stderr.strip()}"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            http_client.delete(f"/v1/sessions/{session_id}", timeout=15.0)
