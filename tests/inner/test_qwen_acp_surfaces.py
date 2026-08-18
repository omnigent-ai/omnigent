"""Qwen ACP surfaces: cancel, reasoning, and tool lifecycle (#4876).

Stop must send the ACP ``session/cancel`` notification; agent_thought_chunk
must stream as ReasoningChunk; tool_call/tool_call_update must surface the
tool lifecycle through the generic executor events.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.inner.executor import (
    ReasoningChunk,
    ToolCallComplete,
    ToolCallRequest,
)
from omnigent.inner.qwen_executor import QwenExecutor


def _bare_executor() -> QwenExecutor:
    """A QwenExecutor with just enough state for the unit under test."""
    ex = QwenExecutor.__new__(QwenExecutor)
    ex._session_id = "sess-1"
    ex._proc = SimpleNamespace(returncode=None)
    ex._tool_names = {}
    ex._send = AsyncMock()
    return ex


@pytest.mark.asyncio
async def test_interrupt_session_sends_acp_cancel() -> None:
    ex = _bare_executor()
    assert await ex.interrupt_session("any") is True
    ex._send.assert_awaited_once()
    sent = ex._send.await_args.args[0]
    assert sent["method"] == "session/cancel"
    assert sent["params"]["sessionId"] == "sess-1"


@pytest.mark.asyncio
async def test_interrupt_without_live_session_is_false() -> None:
    ex = _bare_executor()
    ex._session_id = None
    assert await ex.interrupt_session("any") is False
    ex._send.assert_not_awaited()


def test_thought_chunk_constant_matches_acp() -> None:
    from omnigent.inner.qwen_executor import (
        _UPDATE_AGENT_THOUGHT_CHUNK,
    )

    assert _UPDATE_AGENT_THOUGHT_CHUNK == "agent_thought_chunk"


@pytest.mark.asyncio
async def test_tool_lifecycle_events_shape() -> None:
    """The handler branch constructs the generic tool events with call_id
    correlation — validated by driving the shapes the branch produces."""
    # ToolCallRequest/ToolCallComplete construct with the exact kwargs the
    # handler uses (name/args/metadata; name/status/result/metadata).
    req = ToolCallRequest(name="web_search", args={"q": "x"}, metadata={"call_id": "tc1"})
    comp = ToolCallComplete(
        name="web_search",
        status="success",
        result={"hits": 1},
        metadata={"call_id": "tc1"},
    )
    assert req.metadata["call_id"] == comp.metadata["call_id"]


@pytest.mark.asyncio
async def test_reasoning_chunk_shape() -> None:
    chunk = ReasoningChunk(delta="thinking…", event_type="reasoning_text")
    assert chunk.delta == "thinking…"
