"""Tests for the macOS keep-awake monitor."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from omnigent.util import keep_awake
from omnigent.util.keep_awake import keep_awake_enabled, run_keep_awake_monitor


@pytest.fixture
def fake_caffeinate(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in ``caffeinate`` that records its argv and pid, then sleeps."""
    log = tmp_path / "calls.log"
    script = tmp_path / "caffeinate"
    script.write_text(f'#!/bin/sh\necho "$$ $*" >> {log}\nexec sleep 60\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("darwin", None, True),
        ("darwin", "1", True),
        ("darwin", "0", False),
        ("darwin", "off", False),
        ("linux", None, False),
        ("win32", "1", False),
    ],
)
def test_keep_awake_enabled(
    monkeypatch: pytest.MonkeyPatch, platform: str, env: str | None, expected: bool
) -> None:
    if env is None:
        monkeypatch.delenv(keep_awake.KEEP_AWAKE_ENV, raising=False)
    else:
        monkeypatch.setenv(keep_awake.KEEP_AWAKE_ENV, env)
    assert keep_awake_enabled(platform=platform) is expected


async def test_holds_assertion_only_while_turn_runs(fake_caffeinate: tuple[Path, Path]) -> None:
    script, log = fake_caffeinate
    busy = False
    task = asyncio.create_task(
        run_keep_awake_monitor(
            lambda: busy, runner_id="runner_t", poll_interval_s=0.01, caffeinate_binary=str(script)
        )
    )
    await asyncio.sleep(0.05)
    assert not log.exists()

    busy = True
    await _wait_for(log.exists)
    pid_str, args = log.read_text().split(" ", 1)
    pid = int(pid_str)
    assert args.split() == ["-i", "-w", str(os.getpid())]
    assert _pid_alive(pid)

    busy = False
    await _wait_for(lambda: not _pid_alive(pid))

    busy = True
    await _wait_for(lambda: len(log.read_text().splitlines()) == 2)
    second_pid = int(log.read_text().splitlines()[1].split()[0])

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for(lambda: not _pid_alive(second_pid))


async def test_missing_binary_returns_without_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keep_awake.shutil, "which", lambda _name: None)
    calls = 0

    def _busy() -> bool:
        nonlocal calls
        calls += 1
        return True

    await asyncio.wait_for(run_keep_awake_monitor(_busy, runner_id="runner_t"), timeout=1)
    assert calls == 0


async def test_cancel_during_release_still_reaps_caffeinate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown cancelling a release mid-wait still kills and reaps the child."""
    log = tmp_path / "calls.log"
    script = tmp_path / "caffeinate"
    # Ignores SIGTERM, so every release waits out the stop timeout before kill.
    script.write_text(f"#!/bin/sh\ntrap '' TERM\necho \"$$\" >> {log}\nexec sleep 60\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(keep_awake, "_STOP_TIMEOUT_S", 0.3)
    busy = True
    task = asyncio.create_task(
        run_keep_awake_monitor(
            lambda: busy, runner_id="runner_t", poll_interval_s=0.01, caffeinate_binary=str(script)
        )
    )
    await _wait_for(log.exists)
    await asyncio.sleep(0.05)
    pid = int(log.read_text().split()[0])

    busy = False
    await asyncio.sleep(0.1)
    assert _pid_alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _pid_alive(pid)
