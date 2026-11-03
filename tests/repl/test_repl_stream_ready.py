"""The REPL subscribes to the session stream before it posts a message."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from omnigent_client import OmnigentClient

from omnigent.repl import _repl as repl_mod


def _adapter(stream, monkeypatch: pytest.MonkeyPatch) -> repl_mod._SessionsChatReplAdapter:
    client = Mock(spec=OmnigentClient)
    client.sessions = SimpleNamespace(stream=stream, get=AsyncMock(return_value=object()))
    adapter = repl_mod._SessionsChatReplAdapter(client=client, agent_name="test-agent")
    adapter._session_id = "conv_existing"
    for name in ("_recover_runner_if_needed", "_bind_runner_if_needed"):
        monkeypatch.setattr(adapter, name, AsyncMock())
    monkeypatch.setattr(adapter, "_hydrate_from_session_snapshot", Mock())
    return adapter


async def test_session_is_ready_only_once_the_stream_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps: list[str] = []

    async def stream(session_id: str):
        steps.append(f"subscribe {session_id}")
        # The server registers the subscriber, then sends its ready heartbeat.
        await asyncio.sleep(0.3)
        steps.append("registered")
        yield SimpleNamespace(type="session.heartbeat")
        await asyncio.Event().wait()

    adapter = _adapter(stream, monkeypatch)
    try:
        assert await adapter._ensure_session() == "conv_existing"
        # A post after this point cannot be missed by the subscriber.
        assert steps == ["subscribe conv_existing", "registered"]
    finally:
        assert adapter._stream_task is not None
        adapter._stream_task.cancel()


async def test_server_without_ready_heartbeat_does_not_block_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def silent_stream(session_id: str):
        await asyncio.Event().wait()
        yield  # pragma: no cover - never reached

    monkeypatch.setattr(repl_mod, "_STREAM_READY_TIMEOUT_S", 0.2)
    adapter = _adapter(silent_stream, monkeypatch)
    started = time.monotonic()
    try:
        assert await adapter._ensure_session() == "conv_existing"
        assert time.monotonic() - started < 2.0
        assert adapter._stream_task is not None and not adapter._stream_task.done()
    finally:
        assert adapter._stream_task is not None
        adapter._stream_task.cancel()
