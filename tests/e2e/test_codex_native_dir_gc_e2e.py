"""E2E regression test for unbounded ``~/.omnigent`` growth from orphaned dirs.

Per-session codex-native directories under ``~/.omnigent/codex-native/`` are
created when a host-bound ``codex-native-ui`` session launches, but the only
cleanup path is the runner-side ``_delete_native_bridge_dirs`` invoked on a
clean session delete. A session whose host/runner dies uncleanly (crash,
SIGKILL, host restart mid-run) leaves its directory behind forever — nothing
reaps orphaned dirs on host restart or on any periodic sweep, so the tree
grows without bound (28 GB / 273 dirs observed on one developer host).

This test drives the real user journey: connect a host, create a
codex-native session on it (the per-session dir appears), kill the host
daemon and its runner uncleanly, restart the host, and assert the orphaned
per-session directory is reclaimed within a grace period. On the current
build the directory survives forever, so this test FAILS until a dead-owner
reaper / retention GC lands.

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

from omnigent.codex_native_bridge import bridge_dir_for_bridge_id
from omnigent.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

# How long the harness gets to reclaim an orphaned per-session dir after the
# host restarts. Generous so a slow startup sweep still passes; the bug is
# that no sweep exists at all, so today the dir survives indefinitely.
_GC_GRACE_S = 90.0


def _spawn_host_daemon(*, log_path: Path, live_server: str) -> subprocess.Popen[bytes]:
    """
    Spawn an ``omnigent host`` daemon bound to the test server.

    :param log_path: File that captures the daemon's stderr.
    :param live_server: Test server base URL.
    :returns: The spawned daemon subprocess handle.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
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
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


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


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native dir GC e2e needs `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_orphaned_codex_native_dir_is_reclaimed_after_host_restart(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A codex-native session dir orphaned by an unclean death gets reclaimed.

    Journey: connect host -> create a codex-native session (its per-session
    dir appears under ``~/.omnigent/codex-native/``) -> SIGKILL the host
    daemon and its runner tree (crash) -> restart the host -> the orphaned
    per-session dir must be garbage-collected within a grace period.

    Today nothing reclaims it (cleanup only runs on clean session delete,
    inside the now-dead runner), so this test fails until a dead-owner
    reaper runs on host restart.
    """
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()

    daemon = _spawn_host_daemon(
        log_path=tmp_path / "host-daemon-a.log",
        live_server=live_server,
    )
    session_id: str | None = None
    daemon_b: subprocess.Popen[bytes] | None = None
    try:
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
        session_dir = bridge_dir_for_bridge_id(session_id)
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline and not session_dir.is_dir():
            time.sleep(POLL_INTERVAL_S)
        assert session_dir.is_dir(), (
            f"per-session codex-native dir {session_dir} never appeared — "
            "cannot exercise the GC journey"
        )

        # Unclean death: crash the host daemon and every runner it spawned.
        # No session delete, no graceful shutdown — the orphan scenario.
        _kill_tree_uncleanly(daemon)
        assert session_dir.is_dir(), (
            "sanity: the crash itself must not remove the dir (nothing ran cleanup)"
        )

        # Host restart after the crash — the moment a dead-owner reaper /
        # startup GC pass should notice the orphaned dir and reclaim it.
        daemon_b = _spawn_host_daemon(
            log_path=tmp_path / "host-daemon-b.log",
            live_server=live_server,
        )
        _online_host_id(http_client)

        gc_deadline = time.monotonic() + _GC_GRACE_S
        while time.monotonic() < gc_deadline and session_dir.is_dir():
            time.sleep(1.0)

        assert not session_dir.is_dir(), (
            f"orphaned per-session dir {session_dir} for crashed session "
            f"{session_id} was not garbage-collected within {_GC_GRACE_S:.0f}s "
            "of the host restarting — ~/.omnigent grows without bound"
        )
    finally:
        for proc in (daemon, daemon_b):
            if proc is not None and proc.poll() is None:
                _kill_tree_uncleanly(proc)
        # Best-effort server-side delete so the test leaves no session rows
        # behind; the dir itself is asserted on above.
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
            shutil.rmtree(bridge_dir_for_bridge_id(session_id), ignore_errors=True)
