"""End-to-end recovery when a live harness loses its Unix socket."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

_TEST_HARNESS = "socket-recovery-test"
_TEST_HARNESS_MODULE = "tests.runtime.harnesses._test_harness"


@pytest.fixture
def harness_tmp_parent() -> Iterator[Path]:
    """Provide a short path that stays below the macOS UDS length limit."""
    parent = Path(tempfile.gettempdir()) / f"omni-uds-{uuid.uuid4().hex[:8]}"
    parent.mkdir(mode=0o700)
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


@pytest.mark.posix_only
async def test_next_turn_recovers_when_live_harness_socket_disappears(
    harness_tmp_parent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later turn replaces a live process whose UDS pathname vanished."""
    monkeypatch.setitem(_HARNESS_MODULES, _TEST_HARNESS, _TEST_HARNESS_MODULE)
    manager = HarnessProcessManager(tmp_parent=harness_tmp_parent)
    spawn_env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    try:
        await manager.start()
        conversation_id = "socket-disappeared"
        first_client = await manager.get_client(conversation_id, _TEST_HARNESS, env=spawn_env)
        first_pid = (await first_client.get("/pid")).json()["pid"]
        socket_path = manager.socket_path(conversation_id)

        # Reproduce the production failure: only the bound pathname disappears.
        # The runner and harness process are still alive, so returncode-based
        # crash detection alone cannot notice that future connects will fail.
        socket_path.unlink()
        os.kill(first_pid, 0)

        # Match the several-minute gap between the last successful production
        # turn and the failure. After httpx's five-second keepalive expiry, the
        # next request must connect through the now-missing pathname.
        await asyncio.sleep(6)

        next_turn_client = await manager.get_client(conversation_id, _TEST_HARNESS, env=spawn_env)
        next_pid = (await next_turn_client.get("/pid")).json()["pid"]

        assert next_pid != first_pid
        assert socket_path.exists()
    finally:
        await manager.shutdown()
