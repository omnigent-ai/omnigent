"""E2E regression test for Codex bridge retention after unclean death.

Per-session codex-native directories under ``~/.omnigent/codex-native/`` are
created when a host-bound ``codex-native-ui`` session launches. A session whose
host/runner dies uncleanly leaves bridge credentials, sockets, and hook state
behind alongside the ``codex-home`` rollout that Codex needs to resume the
original thread. Host restart must retain recent bridges, then reclaim the
whole directory after 7 days of inactivity.

This test drives the real user journey: connect a host, create a
codex-native session on it (the per-session dir appears), kill the host
daemon and its runner uncleanly, restart the host to verify the recent bridge
survives intact, then age its activity and restart again to verify expiration.

Run::

    OMNIGENT_E2E_CODEX_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_codex_native_dir_gc_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
import psutil
import pytest

from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

# How long the host gets to reclaim an expired bridge after restart.
_GC_GRACE_S = 90.0


def _spawn_host_daemon(
    *,
    home_dir: Path,
    log_path: Path,
    live_server: str,
) -> subprocess.Popen[bytes]:
    """
    Spawn an ``omnigent host`` daemon bound to the test server.

    :param home_dir: Isolated home containing only this test's bridge state.
    :param log_path: File that captures the daemon's stderr.
    :param live_server: Test server base URL.
    :returns: The spawned daemon subprocess handle.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    codex_home_source = env.get("CODEX_HOME") or str(Path.home() / ".codex")
    env["HOME"] = str(home_dir)
    env["CODEX_HOME"] = codex_home_source
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


def _codex_native_agent_id(client: httpx.Client) -> str:
    """
    Return the durable id of the auto-registered ``codex-native-ui``.

    :param client: HTTP client pointed at the test server.
    :returns: The ``"ag_..."`` id for ``codex-native-ui``.
    :raises AssertionError: If the server did not auto-register it.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == CODEX_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(f"{CODEX_NATIVE_AGENT_NAME!r} not registered on the server")


def _kill_tree_uncleanly(proc: subprocess.Popen[bytes]) -> None:
    """
    SIGKILL a process and every descendant, simulating a crash.

    The host daemon spawns runner processes (which own the native harness);
    killing the whole tree without any graceful shutdown reproduces the
    crashed/orphaned-session scenario the bug report describes.

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
    # Give the kernel a moment to finish reaping the tree so no dying runner
    # races the assertions below.
    _, alive = psutil.wait_procs(children, timeout=15)
    for straggler in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            straggler.kill()


def _expire_codex_bridge_activity(bridge_dir: Path) -> None:
    """Age bridge preparation and rollout activity beyond Codex retention."""
    expired_at = time.time() - codex_native_bridge._ORPHAN_RETENTION_SECONDS - 60
    activity_paths = [bridge_dir / "owner.pid"]
    sessions_dir = bridge_dir / "codex-home" / "sessions"
    if sessions_dir.is_dir():
        activity_paths.extend(sessions_dir.rglob("rollout-*.jsonl"))
    for activity_path in activity_paths:
        if activity_path.exists():
            os.utime(activity_path, (expired_at, expired_at))


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native dir GC e2e needs `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_host_restart_retains_recent_codex_bridge_then_reclaims_it(
    live_server: str,
    http_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An unclean host death retains recent state and later reclaims it whole.

    Journey: connect host -> create a codex-native session (its per-session
    dir appears under ``~/.omnigent/codex-native/``) -> SIGKILL the host
    daemon and its runner tree (crash) -> restart the host -> the recent bridge
    remains intact -> crash again, age it past retention, and restart -> the
    old bridge is removed wholesale.
    """
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(
        codex_native_bridge,
        "_BRIDGE_ROOT",
        home_dir / ".omnigent" / "codex-native",
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
        agent_id = _codex_native_agent_id(http_client)

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

        # The runner prepares the per-session bridge dir (keyed on the
        # session id unless rotated — a fresh session is un-rotated) under
        # ~/.omnigent/codex-native/<sha256(session_id)[:32]>.
        session_dir = codex_native_bridge.bridge_dir_for_bridge_id(session_id)
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline and not session_dir.is_dir():
            time.sleep(POLL_INTERVAL_S)
        assert session_dir.is_dir(), (
            f"per-session codex-native dir {session_dir} never appeared — "
            "cannot exercise the GC journey"
        )
        codex_home = session_dir / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True)
        persistent_sentinel = codex_home / "transcript-preserved.txt"
        persistent_sentinel.write_text("keep", encoding="utf-8")
        stale_runtime = session_dir / "stale-runtime.txt"
        stale_runtime.write_text("keep-until-expiry", encoding="utf-8")

        # Unclean death: crash the host daemon and every runner it spawned.
        # No session delete, no graceful shutdown — the orphan scenario.
        _kill_tree_uncleanly(daemon)
        assert session_dir.is_dir(), (
            "sanity: the crash itself must not remove the dir (nothing ran cleanup)"
        )

        # A normal replacement-runner boundary is recent activity, so the
        # first restart must retain both persistent and runtime bridge state.
        daemon_b_log = tmp_path / "host-daemon-b.log"
        daemon_b = _spawn_host_daemon(
            home_dir=home_dir,
            log_path=daemon_b_log,
            live_server=live_server,
        )
        _wait_for_host_connection(daemon_b, daemon_b_log)
        _online_host_id(http_client)
        assert persistent_sentinel.read_text(encoding="utf-8") == "keep"
        assert stale_runtime.read_text(encoding="utf-8") == "keep-until-expiry"

        # Once the whole bridge is inactive beyond retention, the next host
        # restart should remove it. The session may immediately relaunch and
        # recreate an empty bridge, so sentinel removal proves the old tree was
        # reclaimed rather than requiring the directory to stay absent.
        _kill_tree_uncleanly(daemon_b)
        assert session_dir.is_dir()
        _expire_codex_bridge_activity(session_dir)
        daemon_c_log = tmp_path / "host-daemon-c.log"
        daemon_c = _spawn_host_daemon(
            home_dir=home_dir,
            log_path=daemon_c_log,
            live_server=live_server,
        )
        _wait_for_host_connection(daemon_c, daemon_c_log)
        _online_host_id(http_client)
        gc_deadline = time.monotonic() + _GC_GRACE_S
        while time.monotonic() < gc_deadline and persistent_sentinel.exists():
            time.sleep(1.0)

        assert not persistent_sentinel.exists(), (
            f"expired bridge for crashed session {session_id} was not "
            f"reclaimed within {_GC_GRACE_S:.0f}s of the host restarting"
        )
        assert not stale_runtime.exists(), (
            "whole-directory expiration must remove runtime state with the transcript"
        )
    finally:
        for proc in (daemon, daemon_b, daemon_c):
            if proc is not None and proc.poll() is None:
                _kill_tree_uncleanly(proc)
        # Best-effort server-side delete so the test leaves no session rows
        # behind; the dir itself is asserted on above.
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
            shutil.rmtree(
                codex_native_bridge.bridge_dir_for_bridge_id(session_id),
                ignore_errors=True,
            )
