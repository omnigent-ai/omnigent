"""Direct tmux attach owns only its disposable client subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from omnigent.harnesses.claude_native import main as claude_native
from omnigent.harnesses.codex_native import main as codex_native

_NATIVE_WRAPPERS = [claude_native, codex_native]


class _BlockingAttachProcess:
    """Attach-client double that only exits after the direct kill."""

    returncode: int | None = None

    def __init__(self) -> None:
        self.wait_started = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    async def wait(self) -> int:
        self.wait_started.set()
        if self.returncode is None:
            await asyncio.Event().wait()
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


def _patch_fake_attach_process(
    module: object, monkeypatch: pytest.MonkeyPatch, process: _BlockingAttachProcess
) -> None:
    real_asyncio = asyncio

    async def fake_exec(*_argv: str, **_kwargs: object) -> _BlockingAttachProcess:
        return process

    monkeypatch.setattr(
        module,
        "asyncio",
        SimpleNamespace(**(vars(real_asyncio) | {"create_subprocess_exec": fake_exec})),
    )


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX signal semantics")
@pytest.mark.parametrize("module", _NATIVE_WRAPPERS, ids=["claude", "codex"])
async def test_cancelled_direct_attach_reaps_only_attach_client(
    module: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancellation kills the local attach client, never the tmux backend."""
    real_asyncio = asyncio
    spawned: list[asyncio.subprocess.Process] = []
    backend_pids: list[int] = []
    started = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_exec(*argv: str, env: dict[str, str]) -> asyncio.subprocess.Process:
        captured["argv"] = argv
        captured["env"] = env
        child = await real_asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal, subprocess, sys, time; "
            "backend = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); print(backend.pid, flush=True); time.sleep(60)",
            start_new_session=True,
            stdout=real_asyncio.subprocess.PIPE,
        )
        assert child.stdout is not None
        assert await child.stdout.readline() == b"ready\n"
        backend_pids.append(int((await child.stdout.readline()).strip()))
        spawned.append(child)
        started.set()
        return child

    monkeypatch.setattr(
        module,
        "asyncio",
        SimpleNamespace(**(vars(real_asyncio) | {"create_subprocess_exec": fake_exec})),
    )
    monkeypatch.setattr(module, "_DIRECT_TMUX_ATTACH_TERMINATE_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(module, "_DIRECT_TMUX_ATTACH_KILL_TIMEOUT_S", 0.05, raising=False)

    task = asyncio.create_task(module._attach_direct_tmux(tmp_path / "tmux.sock", "main"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)

        child = spawned[0]
        assert child.returncode == -9
        assert await asyncio.wait_for(child.wait(), timeout=1.0) == -9
        assert psutil.pid_exists(backend_pids[0]), "attach cleanup killed the backend child"
        assert captured["argv"][:3] == ("tmux", "-S", str(tmp_path / "tmux.sock"))
        assert captured["argv"][-3:] == ("attach", "-t", "main")
        assert "TMUX" not in captured["env"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for child in spawned:
            if child.returncode is None:
                child.kill()
            await child.wait()
        for pid in backend_pids:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.TimeoutExpired):
                backend = psutil.Process(pid)
                backend.kill()
                backend.wait(timeout=1)


async def test_codex_direct_attach_normal_exit_does_not_terminate_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Process:
        returncode = 0
        terminate_calls = 0
        kill_calls = 0

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

    process = _Process()

    async def fake_exec(*_argv: str, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr(
        codex_native,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"create_subprocess_exec": fake_exec})),
    )
    await codex_native._attach_direct_tmux(tmp_path / "tmux.sock", "main")
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


async def test_claude_profiler_failure_after_spawn_reaps_attach_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _BlockingAttachProcess()
    _patch_fake_attach_process(claude_native, monkeypatch, process)
    monkeypatch.setattr(claude_native, "_DIRECT_TMUX_ATTACH_TERMINATE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(claude_native, "_DIRECT_TMUX_ATTACH_KILL_TIMEOUT_S", 0.01)

    class _Profiler:
        def __init__(self) -> None:
            self.marks = 0

        def mark(self, *_args: object, **_kwargs: object) -> None:
            self.marks += 1
            if self.marks == 2:
                raise RuntimeError("profiler failed")

    with pytest.raises(RuntimeError, match="profiler failed"):
        await claude_native._attach_direct_tmux(
            tmp_path / "tmux.sock", "main", startup_profiler=_Profiler()
        )
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == -9


async def test_codex_start_event_failure_after_spawn_reaps_attach_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _BlockingAttachProcess()
    _patch_fake_attach_process(codex_native, monkeypatch, process)
    monkeypatch.setattr(codex_native, "_DIRECT_TMUX_ATTACH_TERMINATE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(codex_native, "_DIRECT_TMUX_ATTACH_KILL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(
        codex_native,
        "record_startup_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("event failed")),
    )

    with pytest.raises(RuntimeError, match="event failed"):
        await codex_native._attach_direct_tmux(tmp_path / "tmux.sock", "main")
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == -9


async def test_failed_claude_watcher_does_not_replace_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.terminals import ws_common

    process = _BlockingAttachProcess()
    _patch_fake_attach_process(claude_native, monkeypatch, process)
    monkeypatch.setattr(claude_native, "_DIRECT_TMUX_ATTACH_TERMINATE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(claude_native, "_DIRECT_TMUX_ATTACH_KILL_TIMEOUT_S", 0.01)
    watcher_started = asyncio.Event()

    async def fail_pane_check(_socket_path: str, _target: str) -> bool:
        watcher_started.set()
        raise RuntimeError("watcher failed")

    monkeypatch.setattr(ws_common, "_check_pane_dead_definitive", fail_pane_check)
    task = asyncio.create_task(claude_native._attach_direct_tmux(tmp_path / "tmux.sock", "main"))
    await asyncio.wait_for(watcher_started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.kill_calls == 1
    assert "pane watcher failed during attach cleanup" in caplog.text
