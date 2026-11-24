"""Failure-path tests for the worker liveness wrapper."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from omnigent.inner import _liveness_exec


def test_refresh_census_does_not_adopt_reused_worker_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Mock(pid=43210)
    replacement = Mock(pid=43210)
    replacement.create_time.return_value = 200.0
    replacement.children.return_value = [Mock(pid=43211)]
    census = {43210: 100.0}
    monkeypatch.setattr(_liveness_exec.psutil, "Process", lambda pid: replacement)

    _liveness_exec._refresh_census(child, census)

    assert census == {43210: 100.0}
    replacement.children.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX-only")
def test_detached_reaper_fork_failure_falls_back_to_inline_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Mock()
    signals: list[tuple[int, int]] = []
    pgid = os.getpid()

    monkeypatch.setattr(
        "omnigent.inner._liveness_exec.os.fork",
        Mock(side_effect=OSError("ENOMEM")),
    )
    monkeypatch.setattr("omnigent.inner._liveness_exec._group_members", Mock(return_value=[]))
    monkeypatch.setattr(
        "omnigent.inner._liveness_exec._signal_members",
        lambda group, sig: signals.append((group, sig)),
    )

    _liveness_exec._terminate_group(pgid, child, detached_reaper=True)

    assert signals == [
        (pgid, signal.SIGTERM),
        (pgid, getattr(signal, "SIGKILL", signal.SIGTERM)),
    ]
    child.wait.assert_called_once_with(timeout=1)


# ── Process-level behaviour, exercised with real child processes ────────────
#
# Every child below is started with ``start_new_session=True`` so its process
# group is its own pid: the helpers signal by process group, and the test
# runner's group must never be a target.

posix_only = pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process groups")


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )


def _wait_gone(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> None:
    proc.wait(timeout=timeout)


@posix_only
def test_safe_getpgid_returns_none_for_a_missing_pid() -> None:
    assert _liveness_exec._safe_getpgid(2**22 - 1) is None


@posix_only
def test_group_members_and_signal_members_cover_a_real_process_group() -> None:
    child = _sleeper()
    try:
        members = _liveness_exec._group_members(child.pid)
        assert child.pid in members
        assert os.getpid() not in members  # never the caller, even in a shared group
        _liveness_exec._signal_members(child.pid, signal.SIGTERM)
        _wait_gone(child)
        assert child.returncode == -signal.SIGTERM
    finally:
        child.kill()


@posix_only
def test_refresh_census_records_live_descendants_by_identity() -> None:
    child = _sleeper()
    try:
        census: dict[int, float] = {}
        _liveness_exec._refresh_census(child, census)
        assert census[child.pid] == _liveness_exec._start_stamp(psutil.Process(child.pid))
        assert os.getpid() not in census
        # A root whose recorded identity no longer matches (pid reuse) is not
        # re-walked: the pass bails out and the census is left exactly as it was.
        census[child.pid] = 1.0
        _liveness_exec._refresh_census(child, census)
        assert census == {child.pid: 1.0}
    finally:
        child.kill()
        child.wait(timeout=5)


@posix_only
def test_refresh_census_tolerates_a_vanished_child() -> None:
    child = _sleeper()
    child.kill()
    child.wait(timeout=5)
    census: dict[int, float] = {}
    _liveness_exec._refresh_census(child, census)
    assert census == {}


@posix_only
def test_census_alive_and_signal_census_follow_process_identity() -> None:
    child = _sleeper()
    try:
        census: dict[int, float] = {}
        _liveness_exec._refresh_census(child, census)
        assert _liveness_exec._census_alive(census) is True

        # A stale identity (pid reused by another process) is never signalled.
        stale = {child.pid: census[child.pid] + 1000.0}
        _liveness_exec._signal_census(stale, signal.SIGKILL)
        assert child.poll() is None
        assert _liveness_exec._census_alive(stale) is False

        _liveness_exec._signal_census(census, signal.SIGKILL)
        _wait_gone(child)
        assert child.returncode == -signal.SIGKILL
        assert _liveness_exec._census_alive(census) is False
        # Entries that never existed, or belong to the caller, are skipped.
        _liveness_exec._signal_census({0: 0.0, os.getpid(): 0.0, 2**22 - 1: 0.0}, signal.SIGKILL)
    finally:
        child.kill()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/stat btime is Linux-only")
@pytest.mark.parametrize("step", [-1.0, 1.0])
def test_census_identity_survives_a_wall_clock_step(
    monkeypatch: pytest.MonkeyPatch, step: float
) -> None:
    """An NTP/hypervisor clock step moves btime; the census must still match the worker."""
    import psutil._pslinux as pslinux

    child = _sleeper()
    try:
        census: dict[int, float] = {}
        _liveness_exec._refresh_census(child, census)
        real_boot_time = pslinux.boot_time
        monkeypatch.setattr(pslinux, "boot_time", lambda: real_boot_time() + step)

        assert _liveness_exec._census_alive(census) is True
        _liveness_exec._signal_census(census, signal.SIGKILL)
        _wait_gone(child)
        assert child.returncode == -signal.SIGKILL
    finally:
        child.kill()


@posix_only
def test_terminate_group_inline_escalates_until_the_group_is_gone() -> None:
    child = _sleeper()
    try:
        started = time.monotonic()
        _liveness_exec._terminate_group(child.pid, child)
        assert child.poll() is not None
        assert child.returncode in (-signal.SIGTERM, -signal.SIGKILL)
        assert time.monotonic() - started < 5.0
        assert _liveness_exec._group_members(child.pid) == []
    finally:
        child.kill()


# ── main(): the exec wrapper as a real subprocess ──────────────────────────


def _run_exec(argv: list[str], *, close_liveness_after: float | None) -> subprocess.Popen[bytes]:
    """Start ``_liveness_exec`` around *argv* with a fresh liveness pipe.

    :param close_liveness_after: Seconds after which the write end is closed
        (the parent "dies"); ``None`` keeps it open for the worker's lifetime.
    """
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.inner._liveness_exec",
            "--liveness-fd",
            str(read_fd),
            *argv,
        ],
        pass_fds=[read_fd],
        start_new_session=True,
    )
    os.close(read_fd)
    if close_liveness_after is not None:
        time.sleep(close_liveness_after)
        os.close(write_fd)
    else:
        proc.wait(timeout=20)
        os.close(write_fd)
    return proc


def _group_is_empty(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


@posix_only
def test_main_returns_the_worker_exit_code() -> None:
    proc = _run_exec([sys.executable, "-c", "raise SystemExit(7)"], close_liveness_after=None)
    assert proc.returncode == 7


@posix_only
def test_main_without_a_command_exits_2() -> None:
    proc = _run_exec([], close_liveness_after=None)
    assert proc.returncode == 2


@posix_only
def test_main_reaps_descendants_the_worker_left_behind() -> None:
    # The worker exits 3 while a grandchild keeps sleeping in the same group.
    worker = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "sys.exit(3)"
    )
    proc = _run_exec([sys.executable, "-c", worker], close_liveness_after=None)
    assert proc.returncode == 3
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _group_is_empty(proc.pid):
        time.sleep(0.05)
    assert _group_is_empty(proc.pid), "orphaned grandchild survived the worker's exit"


@posix_only
def test_main_tears_the_group_down_when_the_liveness_pipe_closes() -> None:
    proc = _run_exec(
        [sys.executable, "-c", "import time; time.sleep(60)"], close_liveness_after=0.5
    )
    # The detached reaper signals the whole group, the wrapper included, so the
    # wrapper ends by signal (or returns 1 on the inline fallback) — never 0,
    # and never by outliving its parent.
    proc.wait(timeout=10)
    assert proc.returncode != 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _group_is_empty(proc.pid):
        time.sleep(0.05)
    assert _group_is_empty(proc.pid), "worker survived its parent's death"


# ── main() and the detached reaper, in-process with the group signalling
#    stubbed (the real group is the test runner's own) ──────────────────────


def _main_with(monkeypatch: pytest.MonkeyPatch, argv: list[str], liveness_fd: int) -> int:
    monkeypatch.setattr(sys, "argv", ["_liveness_exec", "--liveness-fd", str(liveness_fd), *argv])
    return _liveness_exec.main()


@posix_only
def test_main_in_process_returns_worker_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setattr(_liveness_exec, "_group_members", lambda pgid: [])
        monkeypatch.setattr(_liveness_exec, "_census_alive", lambda census: False)
        assert _main_with(monkeypatch, [sys.executable, "-c", "raise SystemExit(5)"], read_fd) == 5
    finally:
        os.close(read_fd)
        os.close(write_fd)


@posix_only
def test_main_in_process_reaps_leftovers_with_both_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    members_calls: list[int] = []
    census_calls: list[int] = []
    monkeypatch.setattr(_liveness_exec, "_group_members", lambda pgid: [424242])
    monkeypatch.setattr(_liveness_exec, "_census_alive", lambda census: True)
    monkeypatch.setattr(
        _liveness_exec, "_signal_members", lambda pgid, sig: members_calls.append(sig)
    )
    monkeypatch.setattr(
        _liveness_exec, "_signal_census", lambda census, sig: census_calls.append(sig)
    )
    monkeypatch.setattr(_liveness_exec.time, "sleep", lambda s: None)
    try:
        assert _main_with(monkeypatch, [sys.executable, "-c", "raise SystemExit(0)"], read_fd) == 0
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert members_calls == [signal.SIGTERM, signal.SIGKILL]
    assert census_calls == [signal.SIGTERM, signal.SIGKILL]


@posix_only
def test_main_in_process_tears_down_when_the_pipe_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # the parent is already gone
    torn_down: list[tuple[int, bool]] = []

    def _fake_terminate(pgid: int, child: subprocess.Popen[bytes], **kw: object) -> None:
        torn_down.append((pgid, bool(kw.get("detached_reaper"))))
        child.kill()
        child.wait(timeout=5)

    monkeypatch.setattr(_liveness_exec, "_terminate_group", _fake_terminate)
    try:
        rc = _main_with(
            monkeypatch, [sys.executable, "-c", "import time; time.sleep(60)"], read_fd
        )
    finally:
        os.close(read_fd)
    assert rc == 1
    assert torn_down == [(os.getpgrp(), True)]


@posix_only
def test_main_in_process_tears_down_on_a_broken_liveness_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torn_down: list[int] = []

    def _fake_terminate(pgid: int, child: subprocess.Popen[bytes], **kw: object) -> None:
        torn_down.append(pgid)
        child.kill()
        child.wait(timeout=5)

    monkeypatch.setattr(_liveness_exec, "_terminate_group", _fake_terminate)

    def _broken_select(*args: object, **kwargs: object) -> object:
        raise OSError(9, "Bad file descriptor")

    # A liveness fd the kernel rejects (EBADF) takes the same teardown path.
    monkeypatch.setattr(_liveness_exec.select, "select", _broken_select)
    rc = _main_with(monkeypatch, [sys.executable, "-c", "import time; time.sleep(60)"], 0)
    assert rc == 1
    assert torn_down == [os.getpgrp()]


@posix_only
def test_main_in_process_without_a_command_returns_2(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main_with(monkeypatch, [], 0) == 2


@posix_only
def test_detached_reaper_child_escalates_term_then_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forked reaper (fork → 0) signals TERM, re-walks, then KILLs and exits."""
    child = Mock()
    child.pid = 999999
    killpg_calls: list[int] = []
    census_calls: list[int] = []
    # monotonic(): deadline computed at 0.0, one TERM loop pass at 0.0, then past it.
    ticks = [0.0, 0.0]

    class _Exit(BaseException):
        pass

    def _exit(code: int) -> None:
        raise _Exit()

    # Swap the module's os/time handles for the reaper only, never the real
    # modules: the test runner's event loop keeps its own clock and signals.
    fake_os = SimpleNamespace(
        fork=lambda: 0,
        setsid=lambda: None,
        killpg=lambda pgid, sig: killpg_calls.append(sig),
        _exit=_exit,
        getpid=os.getpid,
    )
    fake_time = SimpleNamespace(
        monotonic=lambda: ticks.pop(0) if ticks else 10.0, sleep=lambda s: None
    )
    monkeypatch.setattr(_liveness_exec, "os", fake_os)
    monkeypatch.setattr(_liveness_exec, "time", fake_time)
    monkeypatch.setattr(_liveness_exec, "_refresh_census", lambda c, census: None)
    monkeypatch.setattr(
        _liveness_exec, "_signal_census", lambda census, sig: census_calls.append(sig)
    )
    with pytest.raises(_Exit):
        _liveness_exec._terminate_group(os.getpid(), child, detached_reaper=True)
    assert killpg_calls == [signal.SIGTERM, signal.SIGKILL]
    assert census_calls == [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL]


@posix_only
def test_detached_reaper_parent_waits_for_the_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The group leader parks in signal.pause() once the reaper owns teardown."""
    child = Mock()
    child.pid = 999999

    class _Parked(BaseException):
        pass

    def _pause() -> None:
        raise _Parked()

    monkeypatch.setattr(
        _liveness_exec, "os", SimpleNamespace(fork=lambda: 12345, getpid=os.getpid)
    )
    monkeypatch.setattr(
        _liveness_exec, "signal", SimpleNamespace(pause=_pause, SIGTERM=signal.SIGTERM)
    )
    monkeypatch.setattr(_liveness_exec, "_refresh_census", lambda c, census: None)
    with pytest.raises(_Parked):
        _liveness_exec._terminate_group(os.getpid(), child, detached_reaper=True)
