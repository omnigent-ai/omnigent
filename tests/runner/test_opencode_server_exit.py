"""The runner notices when its per-session ``opencode serve`` dies on its own."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from omnigent.runner.native import orchestration


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class _Forwarder:
    def __init__(self) -> None:
        self.cancelled = False

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_server_exit_stops_the_forwarder_and_reports_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration, "_OPENCODE_SERVER_EXIT_POLL_S", 0.01)
    process = _Process()
    closed: list[bool] = []

    async def _close() -> None:
        closed.append(True)

    server = SimpleNamespace(process=process, close=_close)
    forwarder = _Forwarder()
    exits: list[int | None] = []

    async def _on_exit(code: int | None) -> None:
        exits.append(code)

    supervisor = asyncio.create_task(
        orchestration._supervise_opencode_forwarder(
            "conv_exit", cast(Any, server), cast(Any, forwarder), on_server_exit=_on_exit
        )
    )
    await asyncio.sleep(0.05)
    assert not supervisor.done()
    process.returncode = -9  # e.g. the kernel OOM killer
    await asyncio.wait_for(supervisor, timeout=2)

    assert exits == [-9]
    assert forwarder.cancelled
    assert closed == [True]


async def test_teardown_cancel_does_not_report_a_server_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration, "_OPENCODE_SERVER_EXIT_POLL_S", 0.01)

    async def _close() -> None:
        return None

    server = SimpleNamespace(process=_Process(), close=_close)
    exits: list[int | None] = []

    async def _on_exit(code: int | None) -> None:
        exits.append(code)

    supervisor = asyncio.create_task(
        orchestration._supervise_opencode_forwarder(
            "conv_teardown", cast(Any, server), cast(Any, _Forwarder()), on_server_exit=_on_exit
        )
    )
    await asyncio.sleep(0.05)
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor
    assert exits == []
