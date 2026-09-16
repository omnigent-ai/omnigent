"""Extended tests for conversation entity types not covered by existing tests.

Covers: ErrorData, CompactionData, NativeToolData, ResourceEventData,
TerminalCommandData, NON_CONTENT_ITEM_TYPES, ITEM_TYPE_TO_DATA_CLS,
_validate_type_matches_data, and Conversation field defaults.
"""

from __future__ import annotations

import json
import sys
from typing import Any

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
    FunctionCallOutputData,
    MessageData,
    NativeToolData,
    NewConversationItem,
    ResourceEventData,
    TerminalCommandData,
    _binary_payload_omitted,
    _validate_type_matches_data,
    parse_item_data,
)

# ── ErrorData ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("level", "expected"), [(None, None), ("info", "info"), ("error", "error")]
)
def test_error_data_level(level: str | None, expected: str | None) -> None:
    kwargs = {} if level is None else {"level": level}
    err = ErrorData(source="harness", code="codex_thread_reset", message="fresh thread", **kwargs)
    assert err.level == expected


def test_error_data_rejects_unknown_level() -> None:
    with pytest.raises(ValidationError):
        ErrorData(source="harness", code="x", message="y", level="warning")  # type: ignore[arg-type]


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


@pytest.mark.parametrize("window_id", [2, "01a070e2-2665-7d62-9b74-973decf239b7"])
def test_compaction_data_accepts_vendor_window_id(window_id: int | str) -> None:
    cd = CompactionData(
        summary="Compacted",
        last_item_id="msg_abc123",
        model="system.ai.gpt-5-6-sol",
        token_count=0,
        window_id=window_id,
    )

    assert cd.window_id == window_id


def test_compaction_data_missing_field() -> None:
    with pytest.raises(ValidationError, match="last_item_id"):
        CompactionData(summary="s", model="m", token_count=1)  # type: ignore[call-arg]


_IMAGE_BASE64 = "iVBORw0KGgo" + "A" * 4000


def _compaction_event(content: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the compaction event body a native forwarder POSTs."""
    return {
        "summary": "[Claude Code compaction]",
        "last_item_id": "msg_abc123",
        "model": "unknown",
        "token_count": 0,
        "compacted_messages": [{"type": "message", "role": "user", "content": content}],
    }


def test_compaction_snapshot_strips_anthropic_source_base64() -> None:
    """A vendor image block is persisted without its base64 payload."""
    event = _compaction_event(
        [
            {"type": "text", "text": "what is in this screenshot?"},
            {
                "type": "image",
                "file_id": "file_abc123",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _IMAGE_BASE64,
                },
            },
        ]
    )

    data = parse_item_data("compaction", {"type": "compaction", **event})

    assert isinstance(data, CompactionData)
    assert data.compacted_messages is not None
    assert _IMAGE_BASE64 not in json.dumps(data.compacted_messages)
    block = data.compacted_messages[0]["content"][1]
    # file_id and media_type survive so the content stays re-fetchable.
    assert block["file_id"] == "file_abc123"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["data"] == "[image/png content omitted from the compaction snapshot]"
    assert data.compacted_messages[0]["content"][0]["text"] == "what is in this screenshot?"


def test_compaction_snapshot_strips_nested_tool_result_image() -> None:
    """A screenshot returned by a tool is nested inside tool_result.content."""
    event = _compaction_event(
        [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _IMAGE_BASE64,
                        },
                    }
                ],
            }
        ]
    )
    # Every message is stripped, not just the first.
    event["compacted_messages"].insert(
        0, {"type": "message", "role": "user", "content": [{"type": "text", "text": "look"}]}
    )

    data = parse_item_data("compaction", {"type": "compaction", **event})

    assert isinstance(data, CompactionData)
    assert data.compacted_messages is not None
    assert len(data.compacted_messages) == 2
    assert _IMAGE_BASE64 not in json.dumps(data.compacted_messages)


def test_compaction_snapshot_strips_document_block() -> None:
    """An attached PDF uses the same source.data shape as an image."""
    event = _compaction_event(
        [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": _IMAGE_BASE64,
                },
            }
        ]
    )

    data = parse_item_data("compaction", {"type": "compaction", **event})

    assert isinstance(data, CompactionData)
    assert data.compacted_messages is not None
    assert _IMAGE_BASE64 not in json.dumps(data.compacted_messages)


def test_compaction_snapshot_strips_inline_data_uri() -> None:
    """The Responses-shaped snapshot codex-native persists is covered too."""
    event = _compaction_event(
        [{"type": "input_image", "image_url": f"data:image/png;base64,{_IMAGE_BASE64}"}]
    )

    data = parse_item_data("compaction", {"type": "compaction", **event})

    assert isinstance(data, CompactionData)
    assert data.compacted_messages is not None
    assert _IMAGE_BASE64 not in json.dumps(data.compacted_messages)


def test_compaction_snapshot_strip_does_not_mutate_input() -> None:
    """Pydantic shares nested dicts with the caller, so never strip in place."""
    event = _compaction_event(
        [{"type": "image", "source": {"type": "base64", "data": _IMAGE_BASE64}}]
    )

    parse_item_data("compaction", {"type": "compaction", **event})

    source = event["compacted_messages"][0]["content"][0]["source"]
    assert source["data"] == _IMAGE_BASE64


def test_compaction_snapshot_strip_is_idempotent() -> None:
    """Compaction rows are re-validated on every read; re-stripping is a no-op."""
    event = _compaction_event(
        [{"type": "image", "source": {"type": "base64", "data": _IMAGE_BASE64}}]
    )

    once = parse_item_data("compaction", {"type": "compaction", **event})
    assert isinstance(once, CompactionData)
    twice = parse_item_data(
        "compaction",
        {"type": "compaction", **once.model_dump()},
    )

    assert isinstance(twice, CompactionData)
    assert twice.compacted_messages == once.compacted_messages


def test_compaction_snapshot_without_messages_is_unchanged() -> None:
    """The summary-only compaction item (no snapshot) still validates."""
    data = parse_item_data(
        "compaction",
        {"type": "compaction", "summary": "s", "last_item_id": "i", "token_count": 3},
    )

    assert isinstance(data, CompactionData)
    assert data.compacted_messages is None


def test_compaction_snapshot_preserves_text_only_messages() -> None:
    """Text snapshots (cursor/hermes forwarders) round-trip untouched."""
    event = _compaction_event([{"type": "input_text", "text": "hello there"}])

    data = parse_item_data("compaction", {"type": "compaction", **event})

    assert isinstance(data, CompactionData)
    assert data.compacted_messages == event["compacted_messages"]


@pytest.mark.parametrize("block_type", [{}, [], 7])
def test_compaction_snapshot_tolerates_non_string_block_type(block_type: object) -> None:
    """An unhashable ``type`` must not 500 the event route it arrives on."""
    event = _compaction_event([{"type": block_type, "text": "hi"}])

    data = parse_item_data("compaction", {"type": "compaction", **event})

    assert isinstance(data, CompactionData)
    assert data.compacted_messages == event["compacted_messages"]


def test_binary_payload_marker_names_the_media_type() -> None:
    """The marker keeps the media type; the length would break idempotency."""
    assert _binary_payload_omitted("image/png", 4011) == (
        "[image/png content omitted from the compaction snapshot]"
    )
    assert _binary_payload_omitted("", 4011) == (
        "[binary content omitted from the compaction snapshot]"
    )


# ── FunctionCallOutputData binary strip ────────────────────

_TOOL_RESULT_MARKER = "[image/png content omitted from the persisted tool result]"


def _image_tool_result_output() -> str:
    """The canonical image-bearing tool result: text + Anthropic image block."""
    return json.dumps(
        [
            {"type": "text", "text": "read-image-ok screenshot.png"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _IMAGE_BASE64,
                },
            },
        ]
    )


def test_tool_result_strips_anthropic_source_base64() -> None:
    """An image returned by a tool is persisted without its base64 payload."""
    data = parse_item_data(
        "function_call_output",
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": _image_tool_result_output(),
        },
    )

    assert isinstance(data, FunctionCallOutputData)
    assert _IMAGE_BASE64 not in data.output
    blocks = json.loads(data.output)
    # Non-binary content survives so the transcript stays readable.
    assert blocks[0] == {"type": "text", "text": "read-image-ok screenshot.png"}
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert blocks[1]["source"]["data"] == _TOOL_RESULT_MARKER


def test_tool_result_strips_block_level_data_field() -> None:
    """The MCP image shape carries bare base64 under a top-level ``data``."""
    output = json.dumps([{"type": "image", "data": _IMAGE_BASE64, "mimeType": "image/png"}])

    fco = FunctionCallOutputData(call_id="call_1", output=output)

    assert _IMAGE_BASE64 not in fco.output
    assert json.loads(fco.output)[0]["mimeType"] == "image/png"


def test_tool_result_strips_data_uri_in_plain_text() -> None:
    """A non-JSON result can still embed the payload as a data: URI."""
    output = f"here is the screenshot: data:image/png;base64,{_IMAGE_BASE64}"

    fco = FunctionCallOutputData(call_id="call_1", output=output)

    assert _IMAGE_BASE64 not in fco.output
    assert fco.output.startswith("here is the screenshot: ")


def test_tool_result_preserves_plain_text_output() -> None:
    """Ordinary tool results round-trip byte-identical."""
    output = "total 8\ndrwxr-xr-x 2 user user 4096 ."

    fco = FunctionCallOutputData(call_id="call_1", output=output)

    assert fco.output == output


def test_tool_result_preserves_json_with_ordinary_data_key() -> None:
    """A ``data`` list key (paginated API results) is not a payload — keep the
    producer's exact serialization, spacing included."""
    output = '{"object": "list", "data": [{"id": "row_1"}, {"id": "row_2"}]}'

    fco = FunctionCallOutputData(call_id="call_1", output=output)

    assert fco.output == output


def test_tool_result_strip_is_idempotent() -> None:
    """Rows are re-validated on every read; re-stripping is a no-op."""
    once = FunctionCallOutputData(call_id="call_1", output=_image_tool_result_output())
    twice = FunctionCallOutputData(call_id="call_1", output=once.output)

    assert twice.output == once.output


def test_tool_result_strips_uppercase_scheme_data_uri() -> None:
    """The data: scheme is case-insensitive; an uppercase DATA: URI must
    not slip past the fast-path guard and persist inline."""
    output = f"here is the screenshot: DATA:image/png;base64,{_IMAGE_BASE64}"

    fco = FunctionCallOutputData(call_id="call_1", output=output)

    assert _IMAGE_BASE64 not in fco.output
    assert fco.output.startswith("here is the screenshot: ")


def test_tool_result_pathological_nesting_does_not_fail_validation() -> None:
    """A deeply nested output must degrade gracefully, never raise out of
    the validator — the row is re-validated on read, so a raise would make
    the stored conversation unloadable."""
    depth = sys.getrecursionlimit() + 100
    output = "[" * depth + '{"type": "image", "data": "payload"}' + "]" * depth

    data = parse_item_data(
        "function_call_output",
        {"type": "function_call_output", "call_id": "call_1", "output": output},
    )

    assert isinstance(data, FunctionCallOutputData)
    assert isinstance(data.output, str)


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
    expected = {
        "compaction",
        "error",
        "resource_event",
        "routing_decision",
        "slash_command",
        "terminal_command",
    }
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
        "routing_decision",
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
    assert api["created_at"] == 1
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
