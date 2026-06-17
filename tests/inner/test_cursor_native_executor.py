"""Unit tests for the cursor-native (ACP) harness's pure logic.

Covers the ``session/update`` → ExecutorEvent mapping, prompt building, the ACP
client's pure request handlers, and harness registration. The live
``cursor-agent acp`` round-trip is exercised by the per-harness e2e gate, not
here, so these tests need no cursor-agent binary or network.
"""

from __future__ import annotations

from omnigent.inner.cursor_acp_client import (
    _auto_allow_permission,
    _read_text_file,
    _write_text_file,
)
from omnigent.inner.cursor_native_executor import (
    CursorNativeExecutor,
    _build_prompt,
    _update_to_events,
)
from omnigent.inner.executor import (
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
)


class TestUpdateToEvents:
    def test_agent_message_chunk_becomes_text_chunk(self) -> None:
        events = _update_to_events(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
        )
        assert events == [TextChunk(text="hi")]

    def test_empty_message_chunk_yields_nothing(self) -> None:
        assert (
            _update_to_events(
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": ""}}
            )
            == []
        )

    def test_thought_chunk_becomes_reasoning(self) -> None:
        events = _update_to_events(
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "ponder"}}
        )
        assert events == [ReasoningChunk(delta="ponder", event_type="reasoning_text")]

    def test_tool_call_becomes_request(self) -> None:
        events = _update_to_events(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call_1",
                "title": "read_file",
                "rawInput": {"path": "a.txt"},
            }
        )
        assert len(events) == 1
        req = events[0]
        assert isinstance(req, ToolCallRequest)
        assert req.name == "read_file"
        assert req.args == {"path": "a.txt"}
        assert req.metadata == {"call_id": "call_1"}

    def test_tool_call_update_completed_becomes_complete(self) -> None:
        events = _update_to_events(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_1",
                "title": "read_file",
                "status": "completed",
                "content": "ok",
            }
        )
        assert len(events) == 1
        done = events[0]
        assert isinstance(done, ToolCallComplete)
        assert done.metadata == {"call_id": "call_1"}

    def test_tool_call_update_failed_is_error_status(self) -> None:
        events = _update_to_events(
            {"sessionUpdate": "tool_call_update", "status": "failed", "title": "sh"}
        )
        assert len(events) == 1
        done = events[0]
        assert isinstance(done, ToolCallComplete)
        assert done.status == ToolCallStatus.ERROR
        assert done.error

    def test_in_progress_tool_update_yields_nothing(self) -> None:
        assert (
            _update_to_events({"sessionUpdate": "tool_call_update", "status": "in_progress"}) == []
        )

    def test_unknown_update_yields_nothing(self) -> None:
        assert _update_to_events({"sessionUpdate": "available_commands_update"}) == []
        assert _update_to_events({"sessionUpdate": "plan"}) == []


class TestBuildPrompt:
    def test_first_turn_prepends_system_prompt(self) -> None:
        prompt = _build_prompt(
            [{"role": "user", "content": "hello"}],
            is_first_turn=True,
            system_prompt="You are helpful.",
        )
        assert prompt == "You are helpful.\n\nhello"

    def test_later_turn_sends_only_latest_user_text(self) -> None:
        prompt = _build_prompt(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "second"},
            ],
            is_first_turn=False,
            system_prompt="You are helpful.",
        )
        assert prompt == "second"

    def test_first_turn_with_history_serializes_conversation(self) -> None:
        prompt = _build_prompt(
            [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "noted"},
                {"role": "user", "content": "now"},
            ],
            is_first_turn=True,
            system_prompt="",
        )
        assert "Conversation so far:" in prompt
        assert "earlier" in prompt and "now" in prompt

    def test_no_user_message_first_turn_returns_system_prompt(self) -> None:
        assert _build_prompt([], is_first_turn=True, system_prompt="SP") == "SP"


class TestExecutorCapabilities:
    def test_capability_flags(self) -> None:
        ex = CursorNativeExecutor()
        assert ex.supports_streaming() is True
        assert ex.supports_tool_calling() is True
        assert ex.handles_tools_internally() is True
        assert ex.supports_live_message_queue() is False

    def test_session_key_prefers_message_session_id(self) -> None:
        ex = CursorNativeExecutor()
        assert (
            ex._session_key([{"role": "user", "content": "x", "session_id": "conv_1"}]) == "conv_1"
        )
        assert ex._session_key([]) == "__default__"


class TestAcpClientHandlers:
    def test_auto_allow_prefers_allow_option(self) -> None:
        out = _auto_allow_permission(
            {
                "options": [
                    {"optionId": "reject", "kind": "reject_once"},
                    {"optionId": "ok", "kind": "allow_once"},
                ]
            }
        )
        assert out == {"outcome": {"outcome": "selected", "optionId": "ok"}}

    def test_auto_allow_falls_back_to_first_option(self) -> None:
        out = _auto_allow_permission({"options": [{"optionId": "only"}]})
        assert out == {"outcome": {"outcome": "selected", "optionId": "only"}}

    def test_auto_allow_cancels_when_no_options(self) -> None:
        assert _auto_allow_permission({"options": []}) == {"outcome": {"outcome": "cancelled"}}

    def test_fs_roundtrip(self, tmp_path) -> None:
        target = tmp_path / "sub" / "note.txt"
        _write_text_file({"path": str(target), "content": "data"})
        assert _read_text_file({"path": str(target)}) == {"content": "data"}


class TestRegistration:
    def test_harness_is_registered(self) -> None:
        from omnigent.runtime.harnesses import _HARNESS_MODULES

        assert _HARNESS_MODULES["cursor-native"] == "omnigent.inner.cursor_native_harness"

    def test_harness_is_allowlisted(self) -> None:
        from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

        assert "cursor-native" in OMNIGENT_HARNESSES

    def test_cursor_native_is_terminal_native(self) -> None:
        # cursor-native launches the cursor-agent TUI in an omnigent terminal
        # (like claude/codex/pi-native), so the runner must treat it as a native
        # terminal harness (no Omnigent history replay; native message handling).
        from omnigent.harness_aliases import is_native_harness

        assert is_native_harness("cursor-native") is True
        assert is_native_harness("native-cursor") is True
