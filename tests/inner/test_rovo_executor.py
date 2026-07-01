"""Unit tests for :class:`omnigent.inner.rovo_executor.RovoExecutor`.

End-to-end tests drive a fake ACP server over stdio (no ``acli`` needed) and
assert the ACP ``session/update`` stream is translated into the expected
:class:`~omnigent.inner.executor.ExecutorEvent` sequence.
"""

from __future__ import annotations

import sys

import pytest

from omnigent.inner.executor import (
    ExecutorConfig,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.inner.rovo_executor import RovoExecutor, _translate_update


def _fake_server_command() -> list[str]:
    return [sys.executable, "-m", "tests.inner._fake_acp_server"]


# --- pure translation unit tests -------------------------------------------


def test_translate_agent_message_chunk_to_text() -> None:
    events = _translate_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
        {},
    )
    assert len(events) == 1
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "hi"


def test_translate_thought_chunk_to_reasoning() -> None:
    events = _translate_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}},
        {},
    )
    assert len(events) == 1
    assert isinstance(events[0], ReasoningChunk)
    assert events[0].delta == "hmm"


def test_translate_empty_text_yields_nothing() -> None:
    assert (
        _translate_update(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": ""}},
            {},
        )
        == []
    )


def test_translate_tool_call_and_completion_pair() -> None:
    names: dict[str, str] = {}
    req = _translate_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "read_file",
            "rawInput": {"path": "a.py"},
        },
        names,
    )
    assert isinstance(req[0], ToolCallRequest)
    assert req[0].name == "read_file"
    assert req[0].args == {"path": "a.py"}
    assert req[0].metadata["call_id"] == "t1"

    done = _translate_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "completed",
            "rawOutput": "ok",
        },
        names,
    )
    assert isinstance(done[0], ToolCallComplete)
    assert done[0].name == "read_file"
    assert done[0].status is ToolCallStatus.SUCCESS


def test_translate_failed_tool_marks_error() -> None:
    names = {"t9": "shell"}
    done = _translate_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "t9", "status": "failed"},
        names,
    )
    assert isinstance(done[0], ToolCallComplete)
    assert done[0].status is ToolCallStatus.ERROR


def test_executor_capability_flags() -> None:
    ex = RovoExecutor()
    assert ex.supports_streaming() is True
    assert ex.supports_tool_calling() is True
    assert ex.handles_tools_internally() is True
    assert ex.supports_live_message_queue() is False


# --- end-to-end via fake ACP server ----------------------------------------


@pytest.mark.asyncio
async def test_run_turn_streams_text_then_turn_complete() -> None:
    ex = RovoExecutor(acli_path=None)
    # Point the executor at the fake server by overriding the command builder.
    ex._command = _fake_server_command  # type: ignore[method-assign]

    messages = [{"role": "user", "content": "say pong", "session_id": "conv-1"}]
    events = [ev async for ev in ex.run_turn(messages, [], "You are a tester.")]
    await ex.close()

    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert text == "PONG"
    assert any(isinstance(e, ReasoningChunk) for e in events)

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    assert completes[0].response == "PONG"
    assert completes[0].usage is None


@pytest.mark.asyncio
async def test_run_turn_reuses_session_across_turns() -> None:
    ex = RovoExecutor()
    ex._command = _fake_server_command  # type: ignore[method-assign]
    messages = [{"role": "user", "content": "one", "session_id": "conv-2"}]

    async for _ in ex.run_turn(messages, [], ""):
        pass
    state = ex._sessions["conv-2"]
    first_session_id = state.session_id

    messages2 = [
        {"role": "user", "content": "one", "session_id": "conv-2"},
        {"role": "assistant", "content": "PONG", "session_id": "conv-2"},
        {"role": "user", "content": "two", "session_id": "conv-2"},
    ]
    async for _ in ex.run_turn(messages2, [], ""):
        pass
    # Same warm ACP session reused (no new session/new).
    assert ex._sessions["conv-2"].session_id == first_session_id
    await ex.close()
    assert ex._sessions == {}


@pytest.mark.asyncio
async def test_run_turn_auto_allows_permission_and_emits_tool_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that triggers session/request_permission must auto-allow and
    surface the tool-call request + completion, then finish cleanly.

    Regression for: ``ACP error -32602: Invalid params (outcome required)`` and
    the follow-on ``unprocessed tool calls`` error when the permission round
    trip was not handled.
    """
    monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
    ex = RovoExecutor()
    ex._command = _fake_server_command  # type: ignore[method-assign]

    messages = [{"role": "user", "content": "read x", "session_id": "conv-perm"}]
    events = [ev async for ev in ex.run_turn(messages, [], "")]
    await ex.close()

    requests = [e for e in events if isinstance(e, ToolCallRequest)]
    completes_tool = [e for e in events if isinstance(e, ToolCallComplete)]
    assert requests and requests[0].name == "read_file"
    assert completes_tool and completes_tool[0].status is ToolCallStatus.SUCCESS

    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert text == "PONG"
    assert any(isinstance(e, TurnComplete) for e in events)


# --- model selection -------------------------------------------------------


@pytest.mark.asyncio
async def test_session_new_parses_nested_models() -> None:
    """session/new's nested ``models`` object populates available + current."""
    ex = RovoExecutor()
    ex._command = _fake_server_command  # type: ignore[method-assign]
    messages = [{"role": "user", "content": "hi", "session_id": "conv-models"}]
    async for _ in ex.run_turn(messages, [], ""):
        pass
    state = ex._sessions["conv-models"]
    assert state.available_models == ["Claude Sonnet 4.6", "Claude Haiku 4.5"]
    assert state.current_model_id == "Claude Sonnet 4.6"
    await ex.close()


@pytest.mark.asyncio
async def test_config_model_triggers_set_model() -> None:
    """A per-request ``config.model`` selects that model via session/set_model."""
    ex = RovoExecutor()
    ex._command = _fake_server_command  # type: ignore[method-assign]
    messages = [{"role": "user", "content": "hi", "session_id": "conv-setmodel"}]
    cfg = ExecutorConfig(model="Claude Haiku 4.5")
    async for _ in ex.run_turn(messages, [], "", config=cfg):
        pass
    assert ex._sessions["conv-setmodel"].current_model_id == "Claude Haiku 4.5"
    await ex.close()


@pytest.mark.asyncio
async def test_spec_model_override_used_when_no_config_model() -> None:
    """The spec default (constructor ``model``) applies when config has none."""
    ex = RovoExecutor(model="Claude Haiku 4.5")
    ex._command = _fake_server_command  # type: ignore[method-assign]
    messages = [{"role": "user", "content": "hi", "session_id": "conv-spec"}]
    async for _ in ex.run_turn(messages, [], ""):
        pass
    assert ex._sessions["conv-spec"].current_model_id == "Claude Haiku 4.5"
    await ex.close()


@pytest.mark.asyncio
async def test_config_model_wins_over_spec_default() -> None:
    """``config.model`` takes precedence over the constructor default."""
    ex = RovoExecutor(model="Claude Haiku 4.5")
    ex._command = _fake_server_command  # type: ignore[method-assign]
    messages = [{"role": "user", "content": "hi", "session_id": "conv-wins"}]
    cfg = ExecutorConfig(model="Claude Sonnet 4.6")
    async for _ in ex.run_turn(messages, [], "", config=cfg):
        pass
    assert ex._sessions["conv-wins"].current_model_id == "Claude Sonnet 4.6"
    await ex.close()


@pytest.mark.asyncio
async def test_no_model_keeps_rovo_default() -> None:
    """With no model configured, Rovo keeps its advertised default."""
    ex = RovoExecutor()
    ex._command = _fake_server_command  # type: ignore[method-assign]
    messages = [{"role": "user", "content": "hi", "session_id": "conv-nomodel"}]
    async for _ in ex.run_turn(messages, [], ""):
        pass
    # The fake server's currentModelId is left untouched.
    assert ex._sessions["conv-nomodel"].current_model_id == "Claude Sonnet 4.6"
    await ex.close()
