"""Regression tests for bounded subprocess cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

from omnigent.inner._subprocess_lifecycle import (
    subprocess_exited,
    terminate_direct_subprocess,
    terminate_subprocess,
)


class _NeverReapedProcess:
    pid = 43210
    returncode = None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_exit_check_does_not_wait_after_exit() -> None:
    proc = Mock(returncode=1)
    assert await subprocess_exited(proc)
    proc.wait.assert_not_called()


async def test_exit_check_allows_delayed_child_notification() -> None:
    proc = Mock(returncode=None)

    async def notify_exit():
        await asyncio.sleep(0)
        proc.returncode = 1
        return 1

    proc.wait = notify_exit
    assert await subprocess_exited(proc)


async def test_exit_check_does_not_hang_on_a_live_child() -> None:
    proc = _NeverReapedProcess()
    assert not await asyncio.wait_for(subprocess_exited(proc, timeout=0.01), timeout=1)


async def test_final_wait_after_kill_is_bounded() -> None:
    proc = _NeverReapedProcess()
    terminate = Mock()
    kill = Mock()

    reaped = await asyncio.wait_for(
        terminate_subprocess(
            proc,
            terminate_timeout=0.01,
            kill_timeout=0.01,
            label="test child",
            terminate_tree=terminate,
            kill_tree=kill,
        ),
        timeout=0.2,
    )

    assert not reaped
    terminate.assert_called_once_with(proc)
    kill.assert_called_once_with(proc)


async def test_direct_cleanup_never_signals_a_process_group() -> None:
    class _AttachProcess:
        pid = 43211
        returncode = None

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.returncode is None:
                await asyncio.Event().wait()
            return self.returncode

    proc = _AttachProcess()

    assert await terminate_direct_subprocess(proc, terminate_timeout=0.01, kill_timeout=0.01)
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert proc.wait_calls == 2
