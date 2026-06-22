"""Extended tests for conversation entity types not covered by existing tests.

Covers: ErrorData, CompactionData, NativeToolData, ResourceEventData,
TerminalCommandData, NON_CONTENT_ITEM_TYPES, ITEM_TYPE_TO_DATA_CLS,
_validate_type_matches_data, and Conversation field defaults.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
    NON_CONTENT_ITEM_TYPES,
    CompactionData,
    Conversation,
    ConversationItem,
    ErrorData,
    FunctionCallData,
    MessageData,
    NativeToolData,
    NewConversationItem,
    ResourceEventData,
    TerminalCommandData,
    _validate_type_matches_data,
    parse_item_data,
)

# ── ErrorData ─────────────────────────────────────────


def test_error_data_valid() -> None:
    err = ErrorData(
        source="execution",
        code="native_terminal_start_failed",
        message="Native Codex requires the 'codex' CLI on PATH.",
    )
    assert err.source == "execution"
    assert err.code == "native_terminal_start_failed"


def test_error_data_strips_whitespace() -> None:
    err = ErrorData(source="llm", code="  rate_limit  ", message="  Too many requests  ")
    assert err.code == "rate_limit"
    assert err.message == "Too many requests"


def test_error_data_rejects_empty_code() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        ErrorData(source="execution", code="", message="Something broke")


def test_error_data_rejects_empty_message() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        ErrorData(source="execution", code="some_code", message="   ")


def test_error_data_rejects_whitespace_only_code() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        ErrorData(source="tool", code="  \t  ", message="msg")


def test_error_data_rejects_invalid_source() -> None:
    with pytest.raises(ValidationError):
        ErrorData(source="unknown", code="c", message="m")  # type: ignore[arg-type]


def test_error_data_all_valid_sources() -> None:
    for source in ("llm", "execution", "tool"):
        err = ErrorData(source=source, code="c", message="m")  # type: ignore[arg-type]
        assert err.source == source


# ── CompactionData ────────────────────────────────────


def test_compaction_data_valid() -> None:
    cd = CompactionData(
        summary="User asked to analyze data. Agent loaded CSV.",
        last_item_id="msg_abc123",
        model="openai/gpt-4o",
        token_count=342,
    )
    assert cd.summary.startswith("User asked")
    assert cd.last_item_id == "msg_abc123"
    assert cd.model == "openai/gpt-4o"
    assert cd.token_count == 342


def test_compaction_data_missing_field() -> None:
    with pytest.raises(ValidationError, match="last_item_id"):
        CompactionData(summary="s", model="m", token_count=1)  # type: ignore[call-arg]


# ── NativeToolData ────────────────────────────────────


def test_native_tool_data_valid() -> None:
    ntd = NativeToolData(
        item={
            "type": "web_search_call",
            "id": "ws_abc",
            "status": "completed",
        }
    )
    assert ntd.item["type"] == "web_search_call"
    assert ntd.item["id"] == "ws_abc"


def test_native_tool_data_empty_item() -> None:
    ntd = NativeToolData(item={})
    assert ntd.item == {}


# ── ResourceEventData ─────────────────────────────────


def test_resource_event_created() -> None:
    red = ResourceEventData(
        event_type="session.resource.created",
        resource_id="terminal_bash_s1",
        resource_type="terminal",
        resource={"id": "terminal_bash_s1", "name": "bash"},
    )
    assert red.event_type == "session.resource.created"
    assert red.resource is not None
    assert red.resource["id"] == "terminal_bash_s1"


def test_resource_event_deleted() -> None:
    red = ResourceEventData(
        event_type="session.resource.deleted",
        resource_id="file_abc123",
        resource_type="file",
    )
    assert red.resource is None


# ── TerminalCommandData ───────────────────────────────


def test_terminal_command_input() -> None:
    tcd = TerminalCommandData(kind="input", input="pwd")
    assert tcd.kind == "input"
    assert tcd.input == "pwd"
    assert tcd.stdout is None
    assert tcd.stderr is None


def test_terminal_command_output() -> None:
    tcd = TerminalCommandData(
        kind="output",
        stdout="/home/user\n",
        stderr="",
    )
    assert tcd.kind == "output"
    assert tcd.input is None
    assert tcd.stdout == "/home/user\n"


def test_terminal_command_invalid_kind() -> None:
    with pytest.raises(ValidationError):
        TerminalCommandData(kind="unknown")  # type: ignore[arg-type]


# ── NON_CONTENT_ITEM_TYPES ───────────────────────────


def test_non_content_item_types_complete() -> None:
    """All expected non-content types are present."""
    expected = {"compaction", "error", "resource_event", "slash_command", "terminal_command"}
    assert expected == NON_CONTENT_ITEM_TYPES


def test_non_content_item_types_is_frozenset() -> None:
    assert isinstance(NON_CONTENT_ITEM_TYPES, frozenset)


# ── ITEM_TYPE_TO_DATA_CLS ────────────────────────────


def test_item_type_map_covers_all_types() -> None:
    expected_types = {
        "message",
        "function_call",
        "function_call_output",
        "error",
        "reasoning",
        "compaction",
        "native_tool",
        "resource_event",
        "slash_command",
        "terminal_command",
    }
    assert set(ITEM_TYPE_TO_DATA_CLS.keys()) == expected_types


# ── _validate_type_matches_data ───────────────────────


def test_validate_type_matches_data_ok() -> None:
    msg = MessageData(role="user", content=[])
    _validate_type_matches_data("message", msg)  # should not raise


def test_validate_type_matches_data_mismatch() -> None:
    msg = MessageData(role="user", content=[])
    with pytest.raises(ValueError, match="requires FunctionCallData, got MessageData"):
        _validate_type_matches_data("function_call", msg)


def test_validate_type_matches_data_unknown_type() -> None:
    msg = MessageData(role="user", content=[])
    with pytest.raises(ValueError, match="unknown item type"):
        _validate_type_matches_data("nonexistent", msg)


# ── parse_item_data extended ──────────────────────────


def test_parse_error_data() -> None:
    data = parse_item_data("error", {"source": "execution", "code": "c", "message": "m"})
    assert isinstance(data, ErrorData)


def test_parse_compaction_data() -> None:
    data = parse_item_data(
        "compaction",
        {"summary": "s", "last_item_id": "id1", "model": "m", "token_count": 10},
    )
    assert isinstance(data, CompactionData)


def test_parse_native_tool_data() -> None:
    data = parse_item_data("native_tool", {"item": {"type": "web_search_call"}})
    assert isinstance(data, NativeToolData)


def test_parse_resource_event_data() -> None:
    data = parse_item_data(
        "resource_event",
        {"event_type": "session.resource.created", "resource_id": "r1", "resource_type": "file"},
    )
    assert isinstance(data, ResourceEventData)


def test_parse_terminal_command_data() -> None:
    data = parse_item_data("terminal_command", {"kind": "input", "input": "ls"})
    assert isinstance(data, TerminalCommandData)


# ── Conversation field defaults ───────────────────────


def test_conversation_all_defaults() -> None:
    conv = Conversation(
        id="conv_1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_1",
    )
    assert conv.kind == "default"
    assert conv.parent_conversation_id is None
    assert conv.agent_id is None
    assert conv.runner_id is None
    assert conv.host_id is None
    assert conv.labels == {}
    assert conv.session_state == {}
    assert conv.session_usage == {}
    assert conv.reasoning_effort is None
    assert conv.model_override is None
    assert conv.cost_control_mode_override is None
    assert conv.harness_override is None
    assert conv.sub_agent_name is None
    assert conv.external_session_id is None
    assert conv.terminal_launch_args is None
    assert conv.workspace is None
    assert conv.git_branch is None
    assert conv.archived is False


def test_conversation_sub_agent() -> None:
    conv = Conversation(
        id="conv_child",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_root",
        kind="sub_agent",
        parent_conversation_id="conv_parent",
        sub_agent_name="summarizer",
    )
    assert conv.kind == "sub_agent"
    assert conv.parent_conversation_id == "conv_parent"
    assert conv.sub_agent_name == "summarizer"


def test_conversation_session_state_independent() -> None:
    """Each Conversation gets its own session_state dict."""
    a = Conversation(id="a", created_at=1, updated_at=1, root_conversation_id="a")
    b = Conversation(id="b", created_at=1, updated_at=1, root_conversation_id="b")
    a.session_state["counter"] = 5
    assert b.session_state == {}


def test_conversation_session_usage_independent() -> None:
    """Each Conversation gets its own session_usage dict."""
    a = Conversation(id="a", created_at=1, updated_at=1, root_conversation_id="a")
    b = Conversation(id="b", created_at=1, updated_at=1, root_conversation_id="b")
    a.session_usage["total_tokens"] = 1000
    assert b.session_usage == {}


# ── ConversationItem.to_api_dict extended ─────────────


def test_to_api_dict_function_call() -> None:
    item = ConversationItem(
        id="item_fc",
        type="function_call",
        status="completed",
        response_id="resp_1",
        created_at=1,
        data=FunctionCallData(
            agent="my-agent", name="search", arguments='{"q": "test"}', call_id="call_1"
        ),
    )
    api = item.to_api_dict()
    assert api["id"] == "item_fc"
    assert api["type"] == "function_call"
    assert api["model"] == "my-agent"  # alias
    assert api["name"] == "search"
    assert api["call_id"] == "call_1"
    assert "created_by" not in api


def test_to_api_dict_error() -> None:
    item = ConversationItem(
        id="item_err",
        type="error",
        status="completed",
        response_id="resp_1",
        created_at=1,
        data=ErrorData(source="execution", code="terminal_fail", message="No CLI"),
    )
    api = item.to_api_dict()
    assert api["source"] == "execution"
    assert api["code"] == "terminal_fail"
    assert api["message"] == "No CLI"


# ── NewConversationItem with new types ────────────────


def test_new_item_error() -> None:
    item = NewConversationItem(
        type="error",
        response_id="resp_1",
        data=ErrorData(source="tool", code="timeout", message="Tool timed out"),
    )
    assert item.type == "error"


def test_new_item_compaction() -> None:
    item = NewConversationItem(
        type="compaction",
        response_id="resp_1",
        data=CompactionData(summary="s", last_item_id="id1", model="m", token_count=10),
    )
    assert item.type == "compaction"


def test_new_item_terminal_command() -> None:
    item = NewConversationItem(
        type="terminal_command",
        response_id="resp_1",
        data=TerminalCommandData(kind="input", input="ls"),
    )
    assert item.type == "terminal_command"
