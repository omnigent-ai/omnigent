"""E2E regression test for antigravity-native bridge retention after unclean death.

Per-session antigravity-native directories under ``~/.omnigent/antigravity-native/``
hold the isolated agy state root (``agy-home/.gemini``) where agy stores the
session's conversation database. A host/runner that dies uncleanly leaves that
directory behind with a dead ``owner.pid``; the next host's startup sweep must
keep the conversation history the session needs to resume, then reclaim the
whole directory after 7 days of inactivity.

Journey: connect a host, create an antigravity-native session on it (the
per-session dir appears and the real agy starts under it), kill the host daemon
and its runner uncleanly, restart the host and check the conversation database
is still there, then crash again, age the bridge past retention, and restart to
check the old bridge is reclaimed.

Run::

    OMNIGENT_E2E_ANTIGRAVITY_NATIVE=1 .venv/bin/python -m pytest \
        tests/e2e/test_antigravity_native_dir_gc_e2e.py -v
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
from collections.abc import Callable
from pathlib import Path

import httpx
import psutil
import pytest

from omnigent.harnesses.antigravity_native import bridge as antigravity_bridge
from omnigent.harnesses.antigravity_native.launch import agy_binary_path
from omnigent.native.native_coding_agents import ANTIGRAVITY_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

try:
    _AGY_BIN: str | None = agy_binary_path()
except RuntimeError:
    _AGY_BIN = None

_SWEEP_COMPLETED_LOG = "host global maintenance stage completed: stage=native_bridge_orphans"
_HOST_LOG_LINE_RE = re.compile(r"This host's log: (\S+)")
# How long the restarted host gets to run its startup sweep.
_SWEEP_GRACE_S = 90.0
# How long the runner's cold start gets to mint agy's own conversation.
_AGY_CONVERSATION_WAIT_S = 60.0


def _spawn_host_daemon(
    *,
    home_dir: Path,
    log_path: Path,
    live_server: str,
) -> subprocess.Popen[bytes]:
    """
    Spawn an ``omnigent host`` daemon bound to the test server.

    :param home_dir: Isolated home containing only this test's bridge state.
    :param log_path: File that captures the daemon's stdout/stderr.
    :param live_server: Test server base URL.
    :returns: The spawned daemon subprocess handle.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    # The host only launches antigravity-native when agy looks signed in; a
    # placeholder key passes that gate, while an operator-supplied key is kept
    # so a signed-in run can exercise real agy.
    env.setdefault("GEMINI_API_KEY", "e2e-readiness-stand-in")
    # The restart's sweep-completion marker logs at INFO; pin the level so an
    # ambient OMNIGENT_LOG_LEVEL cannot suppress the line the test waits on.
    env["OMNIGENT_LOG_LEVEL"] = "INFO"
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    with open(log_path, "w") as log_fh:
        return subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_fh,
            stderr=log_fh,
        )


def _wait_for_host_connection(
    proc: subprocess.Popen[bytes],
    log_path: Path,
    timeout: float = 45.0,
) -> None:
    """Wait until this daemon logs that its own tunnel connected."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"Host daemon exited with {proc.returncode}:\n"
                f"{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        with contextlib.suppress(OSError):
            if "✓ Connected as" in log_path.read_text(encoding="utf-8"):
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host daemon did not connect within {timeout}s")


def _online_host_id(client: httpx.Client, timeout: float = 45.0) -> str:
    """
    Poll ``GET /v1/hosts`` until at least one host is online.

    :param client: HTTP client pointed at the test server.
    :param timeout: Max seconds to wait.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _antigravity_native_agent_id(client: httpx.Client) -> str:
    """
    Return the durable id of the auto-registered ``antigravity-native-ui``.

    :param client: HTTP client pointed at the test server.
    :returns: The ``"ag_..."`` id for ``antigravity-native-ui``.
    :raises AssertionError: If the server did not auto-register it.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == ANTIGRAVITY_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(f"{ANTIGRAVITY_NATIVE_AGENT_NAME!r} not registered on the server")


def _wait_until(condition: Callable[[], bool], timeout: float, failure: str) -> None:
    """Poll *condition* until it holds or *timeout* seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"{failure} (waited {timeout:.0f}s)")


def _kill_tree_uncleanly(proc: subprocess.Popen[bytes]) -> None:
    """
    SIGKILL a process and every descendant, simulating a crash.

    :param proc: The host daemon subprocess handle.
    """
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess):
            child.send_signal(signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=15)
    _, alive = psutil.wait_procs(children, timeout=15)
    for straggler in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            straggler.kill()


def _tmux_server_running(socket_path: str) -> bool:
    """Return whether a tmux server still answers on *socket_path*."""
    result = subprocess.run(
        ["tmux", "-S", socket_path, "list-sessions"],
        check=False,
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0


def _kill_session_tmux_server(bridge_dir: Path) -> None:
    """
    Stop the session's detached tmux server, if the agy terminal advertised one.

    ``_kill_tree_uncleanly`` only reaps the host daemon's process tree, but agy
    runs under a tmux server that daemonizes and reparents away from it. Left
    alive, agy keeps writing conversation state and would refresh timestamps
    after they are aged, so stop it explicitly and confirm it is gone. An
    already-dead server is fine; a server that will not die surfaces as an error.
    """
    info = antigravity_bridge.read_tmux_info(bridge_dir)
    if info is None:
        return
    socket_path = info["socket_path"]
    if not _tmux_server_running(socket_path):
        return
    subprocess.run(
        ["tmux", "-S", socket_path, "kill-server"],
        check=False,
        capture_output=True,
        timeout=15,
    )
    _wait_until(
        lambda: not _tmux_server_running(socket_path),
        15,
        f"session tmux server on {socket_path} did not terminate",
    )


def _host_log_path(daemon_log: Path, home_dir: Path) -> Path:
    """
    Return the host's own log file, as announced on the daemon's stdout.

    The daemon abbreviates a path under its own ``HOME`` to ``~``; resolve that
    against the daemon's isolated home, not this test process's home.
    """
    text = daemon_log.read_text(encoding="utf-8", errors="replace")
    match = _HOST_LOG_LINE_RE.search(text)
    assert match is not None, f"host daemon did not announce its log file:\n{text}"
    announced = match.group(1)
    if announced == "~" or announced.startswith("~/"):
        return home_dir / announced[1:].lstrip("/")
    return Path(announced)


def _sweep_completed(host_log: Path) -> bool:
    """Whether this host's log records a finished native-bridge orphan sweep."""
    try:
        return _SWEEP_COMPLETED_LOG in host_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _conversation_dbs(agy_state_dir: Path) -> list[Path]:
    """agy conversation databases under the session's isolated state root."""
    return sorted((agy_state_dir / "conversations").glob("*.db"))


def _expire_antigravity_bridge_activity(bridge_dir: Path, agy_state_dir: Path) -> None:
    """Age bridge preparation and conversation activity beyond agy retention."""
    expired_at = time.time() - antigravity_bridge._ORPHAN_RETENTION_SECONDS - 60
    activity_paths = [bridge_dir / "owner.pid"]
    conversations_dir = agy_state_dir / "conversations"
    if conversations_dir.is_dir():
        activity_paths.extend(conversations_dir.iterdir())
    for activity_path in activity_paths:
        if activity_path.exists():
            os.utime(activity_path, (expired_at, expired_at))


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_ANTIGRAVITY_NATIVE") != "1"
    or _AGY_BIN is None
    or shutil.which("tmux") is None,
    reason=(
        "antigravity-native dir GC e2e needs the real `agy` CLI and `tmux` on PATH "
        "and OMNIGENT_E2E_ANTIGRAVITY_NATIVE=1 to run"
    ),
)
def test_host_restart_retains_recent_antigravity_bridge_then_reclaims_it(
    live_server: str,
    http_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A host restart retains recent agy conversation history and reclaims it once expired."""
    workspace = tmp_path / "agy_ws"
    workspace.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(
        antigravity_bridge,
        "_BRIDGE_ROOT",
        home_dir / ".omnigent" / "antigravity-native",
    )

    daemon_a_log = tmp_path / "host-daemon-a.log"
    daemon = _spawn_host_daemon(
        home_dir=home_dir,
        log_path=daemon_a_log,
        live_server=live_server,
    )
    session_id: str | None = None
    daemon_b: subprocess.Popen[bytes] | None = None
    daemon_c: subprocess.Popen[bytes] | None = None
    try:
        _wait_for_host_connection(daemon, daemon_a_log)
        host_id = _online_host_id(http_client)
        agent_id = _antigravity_native_agent_id(http_client)

        create = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
            },
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        bridge_dir = antigravity_bridge.bridge_dir_for_bridge_id(session_id)
        owner_marker = bridge_dir / "owner.pid"
        _wait_until(
            owner_marker.is_file,
            120.0,
            f"per-session antigravity-native dir {bridge_dir} never appeared",
        )
        _wait_until(
            lambda: any(bridge_dir.glob("agy-*.log")),
            120.0,
            f"agy never started writing its launch log under {bridge_dir}",
        )
        agy_state_dir = antigravity_bridge.agy_gemini_dir(bridge_dir) / "antigravity-cli"
        with contextlib.suppress(AssertionError):
            _wait_until(
                lambda: bool(_conversation_dbs(agy_state_dir)),
                _AGY_CONVERSATION_WAIT_S,
                "agy minted no conversation",
            )
        history_dbs = _conversation_dbs(agy_state_dir)
        history_origin = "agy-written" if history_dbs else "seeded stand-in"
        if not history_dbs:
            # Without a sign-in agy may mint nothing, so stand in for the database a
            # real turn would have written, where agy keeps it. agy, not bridge
            # preparation, creates ``antigravity-cli/``, so build the parents too.
            conversations_dir = agy_state_dir / "conversations"
            conversations_dir.mkdir(parents=True, exist_ok=True)
            history_dbs = [conversations_dir / f"{uuid.uuid4()}.db"]
            history_dbs[0].write_bytes(b"conversation history")

        _kill_tree_uncleanly(daemon)
        _kill_session_tmux_server(bridge_dir)
        assert all(db.is_file() for db in history_dbs), (
            "sanity: the crash itself must not remove the dir (nothing ran cleanup)"
        )

        daemon_b_log = tmp_path / "host-daemon-b.log"
        daemon_b = _spawn_host_daemon(
            home_dir=home_dir,
            log_path=daemon_b_log,
            live_server=live_server,
        )
        _wait_for_host_connection(daemon_b, daemon_b_log)
        _online_host_id(http_client)
        host_b_log = _host_log_path(daemon_b_log, home_dir)
        _wait_until(
            lambda: _sweep_completed(host_b_log) or not all(db.exists() for db in history_dbs),
            _SWEEP_GRACE_S,
            "restarted host never ran its native bridge orphan sweep",
        )

        missing = [str(db) for db in history_dbs if not db.is_file()]
        assert not missing, (
            f"restarted host deleted the crashed session {session_id}'s agy conversation "
            f"history ({history_origin}) {missing}; bridge dir present: {bridge_dir.is_dir()}"
        )
        assert owner_marker.is_file(), f"bridge dir {bridge_dir} lost its owner marker"

        # The restart runs the sweep but does not resume the crashed session,
        # so the aged dead-owner bridge still looks inactive and is reclaimed.
        _kill_tree_uncleanly(daemon_b)
        _kill_session_tmux_server(bridge_dir)
        assert all(db.is_file() for db in history_dbs)
        _expire_antigravity_bridge_activity(bridge_dir, agy_state_dir)
        daemon_c_log = tmp_path / "host-daemon-c.log"
        daemon_c = _spawn_host_daemon(
            home_dir=home_dir,
            log_path=daemon_c_log,
            live_server=live_server,
        )
        _wait_for_host_connection(daemon_c, daemon_c_log)
        _online_host_id(http_client)
        host_c_log = _host_log_path(daemon_c_log, home_dir)
        _wait_until(
            lambda: _sweep_completed(host_c_log) or not any(db.exists() for db in history_dbs),
            _SWEEP_GRACE_S,
            "restarted host never ran its native bridge orphan sweep",
        )
        remaining = [str(db) for db in history_dbs if db.exists()]
        assert not remaining, (
            f"expired bridge for crashed session {session_id} was not reclaimed within "
            f"{_SWEEP_GRACE_S:.0f}s of the host restarting: {remaining}"
        )
    finally:
        # Best-effort during teardown so a cleanup race cannot mask the result.
        for proc in (daemon, daemon_b, daemon_c):
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(subprocess.SubprocessError, psutil.Error):
                    _kill_tree_uncleanly(proc)
        if session_id is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError, AssertionError):
                _kill_session_tmux_server(antigravity_bridge.bridge_dir_for_bridge_id(session_id))
            with contextlib.suppress(httpx.HTTPError):
                http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
            shutil.rmtree(
                antigravity_bridge.bridge_dir_for_bridge_id(session_id),
                ignore_errors=True,
            )
