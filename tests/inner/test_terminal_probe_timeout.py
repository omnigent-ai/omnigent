"""Stalled tmux clients must not leak or establish terminal death."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance


class StalledClient:
    """A client whose pipes close only once it is killed and reaped."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.killed = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        await self.killed.wait()
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.killed.set()

    async def wait(self) -> int:
        await self.killed.wait()
        self.returncode = -9
        return self.returncode


@pytest.fixture
def terminal(tmp_path: Path) -> TerminalInstance:
    return TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )


@pytest.mark.parametrize("operation", ["capture", "is_alive", "command"])
async def test_cancelled_tmux_client_is_reaped(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    client = StalledClient()

    async def spawn(*args: object, **kwargs: object) -> StalledClient:
        return client

    monkeypatch.setattr(terminal_mod.asyncio, "create_subprocess_exec", spawn)
    if operation == "capture":
        task = asyncio.create_task(terminal._tmux_output("capture-pane", "-p"))
    elif operation == "is_alive":
        task = asyncio.create_task(terminal.is_alive())
    else:
        task = asyncio.create_task(terminal._tmux("send-keys", "hello"))
    try:
        await asyncio.wait_for(client.started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.killed.is_set(), "cancellation abandoned the tmux client"
        assert client.returncode == -9, "client was not reaped"
        assert terminal.running
    finally:
        client.kill()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["session", "pane", "is_alive"])
async def test_timed_out_probe_preserves_unknown_liveness(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    client = StalledClient()

    async def spawn(*args: object, **kwargs: object) -> StalledClient:
        return client

    monkeypatch.setattr(terminal_mod.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_TIMEOUT_SECONDS", 0.01, raising=False)
    if operation == "session":
        probe = terminal._tmux_session_exists_async()
    elif operation == "pane":
        probe = terminal._pane_is_dead_async()
    else:
        probe = terminal.is_alive()
    try:
        result = await asyncio.wait_for(probe, 2)
        assert result is (True if operation == "is_alive" else None)
        assert client.killed.is_set()
        assert client.returncode == -9
        assert terminal.running
    finally:
        client.kill()


@pytest.mark.parametrize("operation", ["session", "pane"])
def test_sync_probe_timeout_is_unknown(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def run(cmd: list[str], **kwargs: object) -> None:
        assert kwargs.get("timeout") is not None, "tmux probe has no timeout"
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(terminal_mod.subprocess, "run", run)
    result = (
        terminal._tmux_session_exists_sync()
        if operation == "session"
        else terminal._pane_is_dead()
    )
    assert result is None
    assert terminal.running


def test_threaded_watcher_recovers_after_repeated_timeouts(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated timeout clients are retried, then normal pane ticks resume."""
    recovered = threading.Event()
    exited = threading.Event()
    calls = 0

    def run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise subprocess.TimeoutExpired(cmd, 0.01)
        output = b"live pane" if "capture-pane" in cmd else b"0"
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")

    monkeypatch.setattr(terminal_mod.subprocess, "run", run)
    monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_START_FAILURE_BACKOFF_SECONDS", 0)
    terminal.start_idle_watcher_thread(
        on_tick=recovered.set, on_exit=exited.set, poll_interval_s=0.01
    )
    try:
        assert recovered.wait(5), "watcher never recovered from stalled clients"
        assert terminal.running
        assert not exited.is_set()
    finally:
        terminal._stop_idle_watcher_thread()


async def test_cancelled_probe_reaps_real_subprocess(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a real pipe read leaves no sleeping client process behind."""
    create_subprocess = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    clients: list[asyncio.subprocess.Process] = []

    async def spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        proc = await create_subprocess(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        clients.append(proc)
        spawned.set()
        return proc

    monkeypatch.setattr(terminal_mod.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(terminal._tmux_output("capture-pane", "-p"))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients[0].returncode is not None
        assert terminal.running
    finally:
        task.cancel()
        for proc in clients:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
        await asyncio.gather(task, return_exceptions=True)


async def test_async_watcher_recovers_after_repeated_timeouts(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async watcher also resumes idle detection after a stalled server."""
    recovered = asyncio.Event()
    exited = asyncio.Event()
    captures = 0

    async def output(*args: str) -> str:
        nonlocal captures
        if args[0] == "capture-pane":
            captures += 1
            if captures <= 4:
                raise terminal_mod._TmuxProbeTimeoutError("stalled client")
            return "live frame"
        return "0"

    monkeypatch.setattr(terminal, "_tmux_output", output)
    monkeypatch.setattr(terminal_mod, "_IDLE_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_START_FAILURE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(terminal_mod, "_IDLE_THRESHOLD_SECONDS", 0)
    terminal.start_idle_watcher(recovered.set, on_exit=exited.set)
    try:
        await asyncio.wait_for(recovered.wait(), 5)
        assert captures > 4
        assert terminal.running
        assert not exited.is_set()
    finally:
        await terminal._stop_idle_watcher()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.posix_only
async def test_stalled_tmux_server_recovers_without_losing_session(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suspended private server survives probe timeouts and resumes normally."""
    base = ["tmux", "-S", str(terminal.socket_path), "-f", os.devnull]
    subprocess.run(
        [*base, "new-session", "-d", "-s", "main", "sleep 300"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    server_pid: int | None = None
    try:
        server_pid = int(
            subprocess.check_output([*base, "display-message", "-p", "#{pid}"], timeout=10)
        )
        assert server_pid > 1
        os.kill(server_pid, signal.SIGSTOP)
        monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_TIMEOUT_SECONDS", 0.1)
        assert await terminal.is_alive()
        assert terminal._tmux_session_exists_sync() is None
        assert terminal.running
        os.kill(server_pid, signal.SIGCONT)
        monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_TIMEOUT_SECONDS", 10)
        assert await terminal.is_alive()
        assert await terminal._tmux_session_exists_async() is True
    finally:
        if server_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGCONT)
        subprocess.run([*base, "kill-server"], check=False, capture_output=True, timeout=10)
