"""E2E regression for terminal lifecycle gaps around pinned tmux probes: drives
the real TerminalInstance and its is_alive() probe against a real private tmux
server whose stall is injected deterministically with SIGSTOP/SIGCONT."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import psutil
import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance

# A pinned read probe must give up within its budget plus scheduling slack.
PROBE_CEILING_S = terminal_mod._TMUX_PROBE_TIMEOUT_SECONDS + 5.0
_WATCHER_POLL_S = 2.0

pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH"),
    pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP/SIGCONT are POSIX-only"),
]


@pytest.fixture
def short_parent() -> Iterator[Path]:
    # tmux's AF_UNIX socket path must stay under ~100 bytes; pytest's tmp_path is longer.
    parent = Path(tempfile.mkdtemp(prefix="omnigent-tmux-", dir="/tmp"))
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


@contextlib.asynccontextmanager
async def _launched_terminal(parent: Path) -> AsyncIterator[TerminalInstance]:
    private_dir = parent / "private"
    private_dir.mkdir()
    instance = TerminalInstance(
        name="agent",
        session_key="main",
        socket_path=parent / "tmux.sock",
        private_dir=private_dir,
        command="sh",
        args=["-c", "echo READY; while :; do sleep 0.2; done"],
        keep_alive_after_exit=True,
    )
    await instance.launch(cwd=parent)
    try:
        yield instance
    finally:
        await instance.close()


@contextlib.contextmanager
def _stalled_server(socket_path: Path) -> Iterator[None]:
    server_pid = int(
        subprocess.run(
            ["tmux", "-S", str(socket_path), "display-message", "-p", "-t", "main", "#{pid}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    )
    os.kill(server_pid, signal.SIGSTOP)
    try:
        yield
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(server_pid, signal.SIGCONT)


def _tmux_clients(socket_path: Path, subcommand: str) -> list[psutil.Process]:
    found: list[psutil.Process] = []
    for child in psutil.Process().children(recursive=True):
        try:
            argv = child.cmdline()
        except psutil.Error:
            continue
        if argv[:1] == ["tmux"] and str(socket_path) in argv and subcommand in argv:
            found.append(child)
    return found


def _pane_is_dead(socket_path: Path) -> bool:
    out = subprocess.run(
        ["tmux", "-S", str(socket_path), "list-panes", "-t", "main", "-F", "#{pane_dead}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    return out.split()[:1] == ["1"]


def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


async def _await_until(predicate: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def test_replaced_watcher_does_not_report_exit_to_the_old_owner(short_parent: Path) -> None:
    """A probe released after the watcher was replaced belongs to nobody."""
    async with _launched_terminal(short_parent) as instance:
        pane_pid = instance.pane_pid_sync()
        assert pane_pid is not None
        old_exit = threading.Event()
        new_exit = threading.Event()

        instance.start_idle_watcher_thread(on_exit=old_exit.set, poll_interval_s=_WATCHER_POLL_S)
        assert _wait_until(lambda: instance.last_pane_text() is not None, 5.0), (
            "watcher never completed its first tick"
        )
        os.kill(pane_pid, signal.SIGKILL)
        assert _wait_until(lambda: _pane_is_dead(instance.socket_path), 3.0), (
            "tmux never reported the killed pane as dead"
        )

        with _stalled_server(instance.socket_path):
            assert _wait_until(
                lambda: bool(_tmux_clients(instance.socket_path, "capture-pane")),
                _WATCHER_POLL_S + 3.0,
            ), "watcher's capture-pane probe never got pinned by the stalled server"
            instance.start_idle_watcher_thread(
                on_exit=new_exit.set, poll_interval_s=_WATCHER_POLL_S, replace=True
            )

        assert new_exit.wait(_WATCHER_POLL_S + 5.0), "new owner never learned of the exit"
        assert not old_exit.is_set(), (
            "the replaced watcher's late pane-death probe fired the OLD owner's on_exit"
        )


async def test_tick_callback_can_rebind_its_own_watcher(short_parent: Path) -> None:
    async with _launched_terminal(short_parent) as instance:
        rebind_errors: list[BaseException] = []
        rebound = threading.Event()
        rebound_ticked = threading.Event()

        def on_tick() -> None:
            if rebound.is_set():
                return
            rebound.set()
            try:
                instance.start_idle_watcher_thread(
                    on_tick=rebound_ticked.set, poll_interval_s=0.05, replace=True
                )
            except BaseException as exc:
                rebind_errors.append(exc)
                raise

        instance.start_idle_watcher_thread(on_tick=on_tick, poll_interval_s=0.05)
        assert rebound.wait(5.0)

        assert rebind_errors == [], f"rebinding from the tick callback raised {rebind_errors!r}"
        assert rebound_ticked.wait(3.0), "the rebound watcher never ticked"
        assert instance._idle_thread is not None and instance._idle_thread.is_alive(), (
            "terminal was left with no idle watcher after the rebind"
        )


async def test_cancelled_liveness_probe_reaps_its_tmux_client(short_parent: Path) -> None:
    async with _launched_terminal(short_parent) as instance:
        with _stalled_server(instance.socket_path):
            probe = asyncio.create_task(instance.is_alive())
            assert await _await_until(
                lambda: bool(_tmux_clients(instance.socket_path, "list-panes")), 5.0
            ), "is_alive() never spawned its list-panes client"

            probe.cancel()
            with pytest.raises(asyncio.CancelledError):
                await probe

            assert await _await_until(
                lambda: not _tmux_clients(instance.socket_path, "list-panes"), 3.0
            ), (
                "cancelled is_alive() abandoned its tmux client: "
                f"{[p.pid for p in _tmux_clients(instance.socket_path, 'list-panes')]}"
            )
            assert instance.running is True

        assert await instance.is_alive() is True


async def test_liveness_probe_is_bounded_when_the_tmux_server_stalls(short_parent: Path) -> None:
    async with _launched_terminal(short_parent) as instance:
        with _stalled_server(instance.socket_path):
            started = time.monotonic()
            try:
                alive = await asyncio.wait_for(instance.is_alive(), PROBE_CEILING_S)
            except TimeoutError:
                pytest.fail(
                    f"is_alive() was still pinned on the stalled tmux server after "
                    f"{time.monotonic() - started:.0f}s — read probes have no timeout"
                )
            assert alive is True, "a timed-out probe must preserve unknown liveness as running"
            assert await _await_until(
                lambda: not _tmux_clients(instance.socket_path, "list-panes"), 3.0
            ), "the timed-out probe did not release its tmux client"
            assert instance.running is True

        assert await instance.is_alive() is True


async def test_threaded_watcher_probe_is_bounded_when_the_tmux_server_stalls(
    short_parent: Path,
) -> None:
    async with _launched_terminal(short_parent) as instance:
        exited = threading.Event()
        instance.start_idle_watcher_thread(on_exit=exited.set, poll_interval_s=0.05)
        assert _wait_until(lambda: instance.last_pane_text() is not None, 5.0)

        with _stalled_server(instance.socket_path):
            assert _wait_until(
                lambda: bool(_tmux_clients(instance.socket_path, "capture-pane")), 5.0
            ), "watcher's capture-pane probe never got pinned by the stalled server"
            started = time.monotonic()
            released = _wait_until(
                lambda: not _tmux_clients(instance.socket_path, "capture-pane"),
                PROBE_CEILING_S,
            )
            assert released, (
                f"watcher capture-pane probe still pinned after "
                f"{time.monotonic() - started:.0f}s — the sync read probe has no timeout"
            )
            assert not exited.is_set(), "a timed-out probe must not be reported as an exit"
            assert instance.running is True

        time.sleep(1.0)
        assert not exited.is_set()
        assert instance.running is True
