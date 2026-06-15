"""Tests for :class:`omnigent.inner.cursor_executor.CursorExecutor`.

The cursor harness drives a persistent ``cursor-agent acp`` session. The
ACP client is replaced with a scripted fake (so no ``cursor-agent`` process
runs), letting us exercise the executor's ``session/update`` → ExecutorEvent
mapping, persistent-session reuse across turns, the ``databricks-*`` model drop,
and interrupt/lifecycle. The real ACP wire protocol is covered separately in
``tests/inner/test_cursor_acp.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import cursor_executor as ce
from omnigent.inner.cursor_executor import (
    CursorExecutor,
    _CursorSessionState,
    _sandbox_mode,
    _update_to_event,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    ExecutorError,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)


def _user(content: str, session_id: str = "conv1") -> Message:
    return {"role": "user", "content": content, "session_id": session_id}


def _make_executor(**kwargs: Any) -> CursorExecutor:
    with patch(
        "omnigent.inner.cursor_executor._find_cursor",
        return_value="/usr/bin/cursor-agent",
    ):
        return CursorExecutor(**kwargs)


def _patch_acp(
    monkeypatch: pytest.MonkeyPatch, scripts: list[list[tuple[str, Any]]]
) -> dict[str, Any]:
    """Replace ``AcpClient`` with a fake that replays *scripts* (one per prompt).

    :returns: A state dict capturing ``new_session_models`` (the model passed to
        each ``session/new``) and ``cancelled`` (whether ``cancel`` was called).
    """
    state: dict[str, Any] = {
        "new_session_models": [],
        "cancelled": False,
        "closed": 0,
        "extra_args": None,
    }

    class _FakeAcp:
        def __init__(
            self,
            path: str,
            *,
            env: dict[str, str],
            cwd: str | None,
            extra_args: list[str] | None = None,
        ) -> None:
            self.running = False
            state["extra_args"] = extra_args

        async def start(self) -> dict[str, Any]:
            self.running = True
            return {"agentCapabilities": {}}

        async def new_session(
            self, *, cwd: str, model: str | None, mcp_servers: Any = None
        ) -> str:
            state["new_session_models"].append(model)
            return "sess-1"

        async def prompt_stream(
            self, session_id: str, blocks: Any
        ) -> AsyncIterator[tuple[str, Any]]:
            for item in scripts.pop(0):
                yield item

        async def cancel(self, session_id: str) -> None:
            state["cancelled"] = True

        async def close(self) -> None:
            self.running = False
            state["closed"] += 1

        def stderr_tail(self) -> str:
            return ""

    monkeypatch.setattr(ce, "AcpClient", _FakeAcp)
    return state


def _chunk(kind: str, text: str) -> tuple[str, dict[str, Any]]:
    return ("update", {"sessionUpdate": kind, "content": {"type": "text", "text": text}})


# ---------------------------------------------------------------------------
# Pure mapping
# ---------------------------------------------------------------------------


def test_update_to_event_maps_message_thought_and_tools() -> None:
    assert isinstance(
        _update_to_event({"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}),
        TextChunk,
    )
    thought = _update_to_event(
        {"sessionUpdate": "agent_thought_chunk", "content": {"text": "hmm"}}
    )
    assert isinstance(thought, ReasoningChunk) and thought.event_type == "reasoning_text"
    req = _update_to_event(
        {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read", "rawInput": {"p": 1}}
    )
    assert isinstance(req, ToolCallRequest) and req.name == "Read" and req.args == {"p": 1}
    done = _update_to_event(
        {"sessionUpdate": "tool_call_update", "status": "completed", "title": "Read"}
    )
    assert isinstance(done, ToolCallComplete)
    # Updates with nothing to surface are skipped.
    assert _update_to_event({"sessionUpdate": "available_commands_update"}) is None
    assert _update_to_event({"sessionUpdate": "current_mode_update"}) is None


def test_sandbox_mode_maps_os_env() -> None:
    assert _sandbox_mode(None) == "disabled"
    assert (
        _sandbox_mode(OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")))
        == "disabled"
    )
    assert (
        _sandbox_mode(
            OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="linux_bwrap"))
        )
        == "enabled"
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_missing_cursor_raises_import_error() -> None:
    with patch("omnigent.inner.cursor_executor._find_cursor", return_value=None):
        with pytest.raises(ImportError, match="cursor-agent"):
            CursorExecutor()


def test_api_key_injected_into_env() -> None:
    executor = _make_executor(api_key="cur_xyz")
    assert executor._env.get("CURSOR_API_KEY") == "cur_xyz"


def test_clean_cursor_env_allows_cursor_prefix_denies_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "cur_secret")
    monkeypatch.setenv("FAKE_HOST_SECRET", "PWNED")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    env = ce._clean_cursor_env()
    assert env.get("CURSOR_API_KEY") == "cur_secret"
    assert "FAKE_HOST_SECRET" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "PATH" in env


# ---------------------------------------------------------------------------
# run_turn
# ---------------------------------------------------------------------------


async def test_run_turn_streams_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    script = [
        ("update", {"sessionUpdate": "agent_thought_chunk", "content": {"text": "planning"}}),
        _chunk("agent_message_chunk", "Hello "),
        _chunk("agent_message_chunk", "world"),
        ("result", {"stopReason": "end_turn"}),
    ]
    _patch_acp(monkeypatch, [script])
    executor = _make_executor()
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Hello ", "world"]
    reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
    assert len(reasoning) == 1 and reasoning[0].delta == "planning"
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    assert completes[0].response == "Hello world"
    assert completes[0].usage is None


async def test_session_reused_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = [
        [_chunk("agent_message_chunk", "one"), ("result", {"stopReason": "end_turn"})],
        [_chunk("agent_message_chunk", "two"), ("result", {"stopReason": "end_turn"})],
    ]
    state = _patch_acp(monkeypatch, scripts)
    executor = _make_executor()
    try:
        _ = [e async for e in executor.run_turn([_user("first")], [], "SYS")]
        _ = [e async for e in executor.run_turn([_user("second")], [], "SYS")]
    finally:
        await executor.close()

    # The session is created once (session/new) and reused on turn 2.
    assert len(state["new_session_models"]) == 1


async def test_databricks_model_dropped_at_session_new(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_acp(
        monkeypatch,
        [[_chunk("agent_message_chunk", "ok"), ("result", {"stopReason": "end_turn"})]],
    )
    executor = _make_executor(model="databricks-claude-sonnet-4-6")
    try:
        _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    # A non-cursor databricks id is dropped → session/new gets model=None.
    assert state["new_session_models"] == [None]


async def test_run_turn_passes_sandbox_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_acp(
        monkeypatch,
        [[_chunk("agent_message_chunk", "ok"), ("result", {"stopReason": "end_turn"})]],
    )
    executor = _make_executor(
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="linux_bwrap"))
    )
    try:
        _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    # os_env sandbox → cursor's own --sandbox mode on the acp launch.
    assert state["extra_args"] == ["--sandbox", "enabled"]


async def test_cursor_model_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_acp(
        monkeypatch,
        [[_chunk("agent_message_chunk", "ok"), ("result", {"stopReason": "end_turn"})]],
    )
    executor = _make_executor(model="gpt-5.4-mini")
    try:
        _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    assert state["new_session_models"] == ["gpt-5.4-mini"]


async def test_tool_call_events(monkeypatch: pytest.MonkeyPatch) -> None:
    script = [
        (
            "update",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "Read",
                "rawInput": {"path": "a.txt"},
            },
        ),
        (
            "update",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "title": "Read",
                "status": "completed",
                "content": [{"type": "text", "text": "data"}],
            },
        ),
        ("result", {"stopReason": "end_turn"}),
    ]
    _patch_acp(monkeypatch, [script])
    executor = _make_executor()
    try:
        events = [e async for e in executor.run_turn([_user("read")], [], "SYS")]
    finally:
        await executor.close()
    assert any(isinstance(e, ToolCallRequest) and e.name == "Read" for e in events)
    assert any(isinstance(e, ToolCallComplete) for e in events)


async def test_error_result_yields_executor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_acp(monkeypatch, [[("error", {"code": -1, "message": "boom"})]])
    executor = _make_executor()
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and errors[0].retryable is True


async def test_interrupt_cancels_and_drops_session(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_acp(monkeypatch, [])
    executor = _make_executor()

    class _Stub:
        running = True

        async def cancel(self, session_id: str) -> None:
            state["cancelled"] = True

        async def close(self) -> None:
            pass

    executor._session_states["conv1"] = _CursorSessionState(client=_Stub(), session_id="s")  # type: ignore[arg-type]
    result = await executor.interrupt_session("conv1")
    assert result is True
    assert state["cancelled"] is True
    assert "conv1" not in executor._session_states


async def test_interrupt_unknown_session_returns_false() -> None:
    executor = _make_executor()
    assert await executor.interrupt_session("nope") is False
