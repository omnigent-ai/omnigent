"""E2E coverage: bare ``omnigent host stop`` must stop the running host.

A managed wrapper (e.g. ``isaac omni``) conveys its server through the
wrapper environment rather than injecting ``--server`` into nested host
management commands, so ``host stop`` reaches the CLI with no server
selector and no ``server:`` key in config. The user's journey:

1. register this machine as a host on a server
   (``omnigent host --background --server <url>``),
2. run ``omnigent host stop`` with no server selector,
3. expect the host they just started to stop.

Today step 2 resolves its target to ``"local"``, matches nothing, prints
``No matching host daemon found.`` and exits 0 — while the host daemon
keeps running and the host stays online. This test drives the real CLI
end-to-end (real detached server, real detached host daemon) and asserts
the user-observable contract: after ``host stop``, the daemon process is
dead and its registry record is gone.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

# First boot of the detached server pays cold imports + DB migrations;
# mirror the generous readiness budget the other host e2e suites use.
_COMMAND_TIMEOUT = 180.0
# The daemon gets SIGTERM + a grace period from `host stop`; allow a loaded
# CI box some slack to observe the process actually exiting.
_STOP_WAIT_TIMEOUT = 30.0

# Interruptible pause for bounded external-state polling (no raw time.sleep).
_POLL_PAUSE = threading.Event()

# Repo root: this file lives at <root>/tests/e2e/omnigent/, so three levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _cli_env(home: Path) -> dict[str, str]:
    """Isolated subprocess environment for the CLI under test.

    ``HOME`` is redirected so the local-server pidfile, daemon registry, and
    sqlite db land under the per-test directory. The suite-level isolation
    vars are stripped: they would otherwise override this test's ``HOME`` and
    (for the config home) could carry a ``server:`` key that changes how a
    bare ``host stop`` resolves its target.

    :param home: Isolated HOME for this test's runtime state.
    :returns: Environment dict for ``subprocess.run``.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("OMNIGENT_DATA_DIR", None)
    env.pop("OMNIGENT_CONFIG_HOME", None)
    # Managed-wrapper deployments name the CLI via this var; it only affects
    # hint spelling, and setting it keeps the run faithful to that journey.
    env["OMNIGENT_WRAPPER_COMMAND"] = "isaac omni"
    # Worktree venvs resolve the package via the checkout, not site-packages,
    # and an ambient PYTHONPATH may carry checkout-relative sdk paths that
    # break under the temp-dir cwd. Pin absolute paths for the repo and its
    # in-tree sdks so every spawned CLI/daemon/server resolves imports.
    env["PYTHONPATH"] = os.pathsep.join(
        str(p)
        for p in (
            _REPO_ROOT,
            _REPO_ROOT / "sdks" / "python-client",
            _REPO_ROOT / "sdks" / "ui",
        )
    )
    return env


def _run_cli(
    omnigent_python: Path,
    env: dict[str, str],
    cwd: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run one ``omnigent`` CLI command to completion.

    :param omnigent_python: Interpreter with omnigent installed.
    :param env: Isolated environment from :func:`_cli_env`.
    :param cwd: Working directory (kept empty of project config so
        ``.omnigent/config.yaml`` discovery finds nothing).
    :param args: CLI arguments, e.g. ``("host", "stop")``.
    :returns: The completed process with captured output.
    """
    return subprocess.run(
        [str(omnigent_python), "-m", "omnigent", *args],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT,
    )


def _local_server_url(home: Path) -> str:
    """Read the detached local server's URL from its pidfile.

    :param home: Isolated HOME passed to the CLI subprocesses.
    :returns: Loopback base URL, e.g. ``"http://127.0.0.1:6767"``.
    :raises AssertionError: If the pidfile is missing or malformed.
    """
    pid_path = home / ".omnigent" / "local_server.pid"
    try:
        lines = pid_path.read_text().strip().splitlines()
        return f"http://127.0.0.1:{int(lines[1])}"
    except (IndexError, OSError, ValueError) as exc:
        raise AssertionError(f"missing or malformed local server pidfile at {pid_path}") from exc


def _daemon_records(home: Path) -> list[dict[str, object]]:
    """Load every daemon registry record under the isolated HOME.

    :param home: Isolated HOME passed to the CLI subprocesses.
    :returns: Parsed JSON records (possibly empty).
    """
    registry = home / ".omnigent" / "daemons"
    records: list[dict[str, object]] = []
    if not registry.is_dir():
        return records
    for path in sorted(registry.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            records.append(raw)
    return records


def _daemon_running(pid: int) -> bool:
    """Whether *pid* is a live (non-zombie) process.

    :param pid: Daemon process id from its registry record.
    :returns: ``True`` while the daemon is actually running.
    """
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_for_daemon_exit(pid: int, *, timeout: float) -> bool:
    """Poll until *pid* exits, bounded by *timeout*.

    :param pid: Daemon process id to watch.
    :param timeout: Maximum seconds to wait.
    :returns: ``True`` when the process exited within the budget.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _daemon_running(pid):
            return True
        _POLL_PAUSE.wait(0.2)
    return not _daemon_running(pid)


def test_bare_host_stop_stops_the_running_host_daemon(
    omnigent_python: Path,
    tmp_path: Path,
) -> None:
    """``host stop`` with no server selector stops the host the user started.

    Registers a real detached host daemon against a real detached local
    server (addressed by URL, i.e. a server-target daemon — the shape a
    managed ``--server`` enrollment produces), then runs a bare
    ``host stop`` exactly as a wrapper-mediated invocation delivers it:
    no ``--server`` flag and no ``server:`` config key. The command must
    tear the daemon down; leaving it running while claiming no daemon
    matched is the reported failure.
    """
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    env = _cli_env(home)

    try:
        # Step 0 (precondition): a server for the host to register on. Its
        # loopback URL stands in for the managed remote server — the daemon
        # is keyed by URL target either way.
        boot = _run_cli(omnigent_python, env, workdir, "server", "--background")
        assert boot.returncode == 0, (
            f"server --background failed (rc={boot.returncode}):\n{boot.stdout}\n{boot.stderr}"
        )
        server_url = _local_server_url(home)

        # Step 1: the user registers this machine as a host on that server.
        connect = _run_cli(
            omnigent_python,
            env,
            workdir,
            "host",
            "--background",
            "--non-interactive",
            "--server",
            server_url,
        )
        assert connect.returncode == 0, (
            f"host --background failed (rc={connect.returncode}):\n"
            f"{connect.stdout}\n{connect.stderr}"
        )
        records = [r for r in _daemon_records(home) if r.get("target") == server_url]
        assert records, (
            f"no daemon record registered for {server_url}; records: {_daemon_records(home)}"
        )
        daemon_pid = int(records[0]["pid"])  # type: ignore[arg-type]
        assert _daemon_running(daemon_pid), f"host daemon pid {daemon_pid} is not running"

        # Step 2: the user stops the host — no --server (the wrapper does not
        # inject one into nested host commands) and no configured server.
        stop = _run_cli(omnigent_python, env, workdir, "host", "stop")
        assert stop.returncode == 0, (
            f"host stop failed (rc={stop.returncode}):\n{stop.stdout}\n{stop.stderr}"
        )

        # Step 3: the host they just started must actually be stopped.
        if not _wait_for_daemon_exit(daemon_pid, timeout=_STOP_WAIT_TIMEOUT):
            pytest.fail(
                "bare `host stop` left the host daemon running "
                f"(pid {daemon_pid}, target {server_url}).\n"
                f"host stop output:\n{stop.stdout}\n{stop.stderr}"
            )
        leftover = [r for r in _daemon_records(home) if r.get("target") == server_url]
        assert not leftover, (
            f"bare `host stop` left the daemon registry record behind: {leftover}\n"
            f"host stop output:\n{stop.stdout}\n{stop.stderr}"
        )
    finally:
        # Tear down whatever survived (daemon and/or detached server) so a
        # failing run does not leak processes past the test.
        _run_cli(omnigent_python, env, workdir, "stop", "--force")
