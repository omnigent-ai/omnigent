"""Regression tests for bounded subprocess cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

from omnigent.inner._subprocess_lifecycle import terminate_subprocess


class _NeverReapedProcess:
    pid = 43210
    returncode = None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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
