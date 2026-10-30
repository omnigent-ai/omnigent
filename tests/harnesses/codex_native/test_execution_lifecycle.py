"""Turn-scoped lifecycle for a managed Codex exec-server command."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.execution_environment import ManagedExecServerConfig
from omnigent.harnesses.codex_native.execution_lifecycle import (
    ManagedExecServerController,
    ManagedExecServerError,
)


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _listener_command(port: int, starts: Path) -> tuple[str, ...]:
    script = (
        "import pathlib,socket,sys;"
        "p=pathlib.Path(sys.argv[2]);"
        "p.write_text((p.read_text() if p.exists() else '')+'start\\n');"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        "s.bind(('127.0.0.1',int(sys.argv[1])));s.listen();"
        "\nwhile True:\n c,_=s.accept();c.close()"
    )
    return (sys.executable, "-c", script, str(port), str(starts))


async def _tcp_probe(port: int) -> None:
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()
    await writer.wait_closed()


def test_controller_shares_active_process_then_restarts_after_idle(tmp_path: Path) -> None:
    """Overlapping turns share one command; the next idle-to-active edge starts another."""

    async def scenario() -> tuple[int, int]:
        port = _unused_port()
        starts = tmp_path / "starts.txt"
        controller = ManagedExecServerController(
            ManagedExecServerConfig(
                command=_listener_command(port, starts),
                ready_timeout_seconds=5.0,
            ),
            termination_grace_seconds=1.0,
        )

        first, second = await asyncio.gather(
            controller.acquire(lambda: _tcp_probe(port)),
            controller.acquire(lambda: _tcp_probe(port)),
        )
        first_pid = controller.process_pid
        assert first_pid is not None
        assert starts.read_text().splitlines() == ["start"]

        await controller.release(first)
        assert controller.process_pid == first_pid
        await controller.release(second)
        assert controller.process_pid is None

        third = await controller.acquire(lambda: _tcp_probe(port))
        second_pid = controller.process_pid
        assert second_pid is not None and second_pid != first_pid
        assert starts.read_text().splitlines() == ["start", "start"]
        await controller.release(third)
        await controller.close()
        return first_pid, second_pid

    first_pid, second_pid = asyncio.run(scenario())
    assert first_pid != second_pid


def test_controller_fails_closed_when_command_exits_before_ready() -> None:
    """A dead launcher never grants a lease that would let Codex fall back locally."""

    async def scenario() -> None:
        controller = ManagedExecServerController(
            ManagedExecServerConfig(
                command=(
                    sys.executable,
                    "-c",
                    "import sys; print('remote unavailable', file=sys.stderr); sys.exit(23)",
                ),
                ready_timeout_seconds=2.0,
            )
        )

        async def unavailable() -> None:
            raise ConnectionError("not listening")

        with pytest.raises(ManagedExecServerError, match="remote unavailable"):
            await controller.acquire(unavailable)
        assert controller.process_pid is None
        await controller.close()

    asyncio.run(scenario())


def test_controller_discards_stale_leases_after_managed_process_dies(tmp_path: Path) -> None:
    """A crashed command cannot pin its replacement after the replacement turn ends."""

    async def scenario() -> None:
        port = _unused_port()
        controller = ManagedExecServerController(
            ManagedExecServerConfig(
                command=_listener_command(port, tmp_path / "starts.txt"),
                ready_timeout_seconds=5.0,
            ),
            termination_grace_seconds=1.0,
        )

        stale_lease = await controller.acquire(lambda: _tcp_probe(port))
        first_pid = controller.process_pid
        assert first_pid is not None
        os.kill(first_pid, signal.SIGTERM)
        for _ in range(100):
            if controller.process_pid is None:
                break
            await asyncio.sleep(0.01)
        assert controller.process_pid is None

        replacement_lease = await controller.acquire(lambda: _tcp_probe(port))
        assert controller.process_pid not in {None, first_pid}
        await controller.release(replacement_lease)
        assert controller.process_pid is None
        await controller.release(stale_lease)
        await controller.close()

    asyncio.run(scenario())
