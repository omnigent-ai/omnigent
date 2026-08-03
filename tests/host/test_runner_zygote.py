"""Tests for the runner zygote forkserver and its host-side client.

These drive a REAL zygote subprocess (``os.fork`` works on macOS and Linux even
though the copy-on-write memory savings are Linux-only), using the zygote's
test seam so forked children exit deterministically instead of booting a real
runner. Fork-safety, the ping/fork/poll/reap protocol, env isolation, and the
daemon's Popen fallback are all covered.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from omnigent.host.runner_zygote import (
    ZygoteManager,
    ZygoteRunnerProc,
    ZygoteUnavailable,
)
from omnigent.runner._zygote import _ZYGOTE_TEST_CHILD_EXIT_ENV_VAR


def _fork_env(exit_code: int) -> dict[str, str]:
    """A fork payload env whose child exits with *exit_code* via the test seam.

    :param exit_code: Code the forked child should exit with.
    :returns: Minimal runner env carrying the test-seam marker.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        _ZYGOTE_TEST_CHILD_EXIT_ENV_VAR: str(exit_code),
    }


@pytest.fixture
def manager(tmp_path):
    """A started :class:`ZygoteManager`, stopped on teardown.

    :param tmp_path: Pytest temp dir for the zygote's own log.
    :returns: A running manager connected to a real zygote subprocess.
    """
    mgr = ZygoteManager(log_path=tmp_path / "zygote.log")
    mgr.start()
    try:
        yield mgr
    finally:
        mgr.stop()


def _wait_exit(proc: ZygoteRunnerProc, timeout: float = 10.0) -> int:
    """Poll *proc* until it reports an exit code.

    :param proc: The forked runner handle.
    :param timeout: Max seconds to wait.
    :returns: The observed exit code.
    :raises AssertionError: If the child does not exit in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            return code
        time.sleep(0.05)
    raise AssertionError("forked runner did not exit in time")


def test_import_graph_is_single_threaded() -> None:
    """The runner import graph starts no threads — the fork-safety invariant.

    A zygote forks from this state, so any import-time thread would risk child
    deadlocks. Asserted in a FRESH interpreter (not the shared pytest process,
    which carries its own fixture/timeout threads) so the check reflects only
    the import graph — exactly the state the zygote forks from.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.runner._zygote import _import_runner_graph;"
            "import threading;"
            "_import_runner_graph();"
            "print(threading.active_count());"
            "print([t.name for t in threading.enumerate()])",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    first_line = result.stdout.strip().splitlines()[0]
    assert first_line == "1", result.stdout


def test_manager_starts_and_pings(manager: ZygoteManager) -> None:
    """A started manager has a live zygote pid answering the control socket.

    :param manager: The started manager fixture.
    """
    assert manager.is_running()
    assert isinstance(manager.pid, int)


def test_fork_runner_reports_pid_and_exit_code(manager: ZygoteManager, tmp_path) -> None:
    """A forked runner reports a distinct pid and its real exit code.

    :param manager: The started manager fixture.
    :param tmp_path: Temp dir for the child's log.
    """
    proc = manager.fork_runner(_fork_env(0), str(tmp_path / "runner.log"))
    assert isinstance(proc, ZygoteRunnerProc)
    assert proc.pid != manager.pid
    assert _wait_exit(proc) == 0


def test_fork_runner_nonzero_exit_is_reported(manager: ZygoteManager, tmp_path) -> None:
    """A non-zero child exit surfaces through poll(), matching Popen semantics.

    :param manager: The started manager fixture.
    :param tmp_path: Temp dir for the child's log.
    """
    proc = manager.fork_runner(_fork_env(7), str(tmp_path / "runner.log"))
    assert _wait_exit(proc) == 7


def test_zygote_serves_multiple_forks(manager: ZygoteManager, tmp_path) -> None:
    """The zygote keeps serving fork requests after a child has exited.

    :param manager: The started manager fixture.
    :param tmp_path: Temp dir for the child logs.
    """
    first = manager.fork_runner(_fork_env(0), str(tmp_path / "a.log"))
    assert _wait_exit(first) == 0
    second = manager.fork_runner(_fork_env(3), str(tmp_path / "b.log"))
    assert second.pid != first.pid
    assert _wait_exit(second) == 3


def test_signal_of_exited_runner_is_a_safe_noop(manager: ZygoteManager, tmp_path) -> None:
    """terminate()/kill() on an already-exited runner must not raise.

    The daemon's stop/cleanup paths signal handles that may have exited between
    the poll and the signal, so this must be swallowed.

    :param manager: The started manager fixture.
    :param tmp_path: Temp dir for the child's log.
    """
    proc = manager.fork_runner(_fork_env(0), str(tmp_path / "runner.log"))
    assert _wait_exit(proc) == 0
    proc.terminate()
    proc.kill()


def test_poll_after_stop_returns_none(manager: ZygoteManager, tmp_path) -> None:
    """Polling once the zygote is stopped reports 'still live', never crashes.

    When the control socket is gone the manager cannot learn the exit code, so
    it returns None (the runner's own orphan watchdog handles teardown).

    :param manager: The started manager fixture.
    :param tmp_path: Temp dir for the child's log.
    """
    proc = manager.fork_runner(_fork_env(0), str(tmp_path / "runner.log"))
    manager.stop()
    assert proc.poll() is None


def test_fork_after_stop_raises_unavailable(manager: ZygoteManager, tmp_path) -> None:
    """Forking after stop() raises ZygoteUnavailable so the daemon can fall back.

    :param manager: The started manager fixture.
    :param tmp_path: Temp dir for the child's log.
    """
    manager.stop()
    with pytest.raises(ZygoteUnavailable):
        manager.fork_runner(_fork_env(0), str(tmp_path / "runner.log"))


def test_child_env_is_isolated_between_forks(manager: ZygoteManager, tmp_path) -> None:
    """Each fork's env fully replaces the child environment (no cross-leak).

    The test-seam child echoes its view of ``OMNIGENT_ZYGOTE_MARKER`` to its
    log; two forks with different markers must each see only their own value.

    :param manager: The started manager fixture.
    :param tmp_path: Temp dir for the child logs.
    """
    log_a = tmp_path / "a.log"
    log_b = tmp_path / "b.log"
    env_a = _fork_env(0)
    env_a["OMNIGENT_ZYGOTE_MARKER"] = "aaa"
    env_b = _fork_env(0)
    env_b["OMNIGENT_ZYGOTE_MARKER"] = "bbb"

    proc_a = manager.fork_runner(env_a, str(log_a))
    assert _wait_exit(proc_a) == 0
    proc_b = manager.fork_runner(env_b, str(log_b))
    assert _wait_exit(proc_b) == 0

    assert proc_a.pid != proc_b.pid
    assert "marker=aaa" in log_a.read_text()
    assert "marker=bbb" in log_b.read_text()


def test_unstarted_manager_reports_not_running() -> None:
    """A manager that was never started is not running and has no pid."""
    mgr = ZygoteManager()
    assert not mgr.is_running()
    assert mgr.pid is None
