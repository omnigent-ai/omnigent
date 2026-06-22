"""Tests for runtime data models and API-layer models."""

import pytest
from pydantic import ValidationError

from omnigent.entities import (
    NON_CONTENT_ITEM_TYPES,
    Conversation,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
    ReasoningData,
    SlashCommandData,
    parse_item_data,
)

# ── MessageData ────────────────────────────────────────


def test_user_message() -> None:
    msg = MessageData(role="user", content=[{"type": "input_text", "text": "hi"}])
    assert msg.role == "user"
    assert msg.agent is None


def test_assistant_message() -> None:
    msg = MessageData(
        role="assistant",
        agent="my-agent",
        content=[{"type": "output_text", "text": "hello"}],
    )
    assert msg.role == "assistant"
    assert msg.agent == "my-agent"


def test_assistant_requires_agent() -> None:
    with pytest.raises(ValidationError, match="assistant messages require 'agent'"):
        MessageData(role="assistant", content=[])


def test_user_message_excludes_none_agent() -> None:
    msg = MessageData(role="user", content=[])
    dumped = msg.model_dump(exclude_none=True)
    assert "agent" not in dumped
    assert dumped == {"role": "user", "content": []}


def test_user_message_excludes_default_false_is_meta() -> None:
    """Default ``is_meta=False`` is omitted to avoid API payload churn."""
    msg = MessageData(role="user", content=[])
    dumped = msg.model_dump(exclude_none=True)
    assert "is_meta" not in dumped
    assert dumped == {"role": "user", "content": []}


def test_user_message_includes_true_is_meta() -> None:
    """Hidden durable user messages persist ``is_meta=True``."""
    msg = MessageData(role="user", content=[], is_meta=True)
    assert msg.is_meta is True
    dumped = msg.model_dump(exclude_none=True)
    assert dumped == {"role": "user", "content": [], "is_meta": True}


def test_assistant_message_excludes_default_false_interrupted() -> None:
    """Default ``interrupted=False`` is omitted to avoid API payload churn."""
    msg = MessageData(role="assistant", agent="my-agent", content=[])
    dumped = msg.model_dump(exclude_none=True)
    assert "interrupted" not in dumped
    assert dumped == {"role": "assistant", "agent": "my-agent", "content": []}


def test_assistant_message_includes_true_interrupted() -> None:
    """Interrupted partial assistant messages persist ``interrupted=True``."""
    msg = MessageData(role="assistant", agent="my-agent", content=[], interrupted=True)
    assert msg.interrupted is True
    dumped = msg.model_dump(exclude_none=True)
    assert dumped == {
        "role": "assistant",
        "agent": "my-agent",
        "content": [],
        "interrupted": True,
    }


def test_assistant_message_includes_agent() -> None:
    msg = MessageData(role="assistant", agent="my-agent", content=[])
    dumped = msg.model_dump(exclude_none=True)
    assert dumped == {"role": "assistant", "agent": "my-agent", "content": []}


def test_serialization_alias() -> None:
    msg = MessageData(role="assistant", agent="my-agent", content=[])
    dumped = msg.model_dump(exclude_none=True, by_alias=True)
    assert dumped == {"role": "assistant", "model": "my-agent", "content": []}


def test_invalid_role() -> None:
    with pytest.raises(ValidationError):
        MessageData(role="system", content=[])


# ── FunctionCallData ───────────────────────────────────


def test_function_call_valid() -> None:
    fc = FunctionCallData(
        agent="my-agent",
        name="get_weather",
        arguments='{"city": "SF"}',
        call_id="call_1",
    )
    assert fc.name == "get_weather"
    assert fc.call_id == "call_1"


def test_function_call_missing_call_id() -> None:
    with pytest.raises(ValidationError, match="call_id"):
        FunctionCallData(agent="a", name="b", arguments="c")


def test_function_call_missing_agent() -> None:
    with pytest.raises(ValidationError, match="agent"):
        FunctionCallData(name="b", arguments="c", call_id="d")


# ── FunctionCallOutputData ─────────────────────────────


def test_function_call_output_valid() -> None:
    fco = FunctionCallOutputData(call_id="call_1", output='{"temp": 72}')
    assert fco.call_id == "call_1"


def test_function_call_output_missing_output() -> None:
    with pytest.raises(ValidationError, match="output"):
        FunctionCallOutputData(call_id="call_1")


# ── ReasoningData ──────────────────────────────────────


def test_reasoning_valid_minimal() -> None:
    r = ReasoningData(agent="my-agent", summary=[{"type": "summary_text", "text": "..."}])
    assert r.content is None
    assert r.encrypted_content is None


def test_reasoning_valid_full() -> None:
    r = ReasoningData(
        agent="my-agent",
        summary=[],
        content=[{"type": "text", "text": "thinking..."}],
        encrypted_content="enc_abc",
    )
    assert r.content is not None
    assert r.encrypted_content == "enc_abc"


def test_reasoning_missing_agent() -> None:
    with pytest.raises(ValidationError, match="agent"):
        ReasoningData(summary=[])


# ── NewConversationItem ────────────────────────────────


def test_new_item_user_message() -> None:
    item = NewConversationItem(
        type="message",
        response_id="resp_1",
        data=MessageData(role="user", content=[]),
    )
    assert item.type == "message"
    assert item.data.role == "user"


def test_new_item_assistant_message() -> None:
    item = NewConversationItem(
        type="message",
        response_id="resp_1",
        data=MessageData(role="assistant", agent="my-agent", content=[]),
    )
    assert item.data.agent == "my-agent"


def test_new_item_function_call() -> None:
    item = NewConversationItem(
        type="function_call",
        response_id="resp_1",
        data=FunctionCallData(agent="my-agent", name="fn", arguments="{}", call_id="c1"),
    )
    assert item.data.name == "fn"


def test_new_item_function_call_output() -> None:
    item = NewConversationItem(
        type="function_call_output",
        response_id="resp_1",
        data=FunctionCallOutputData(call_id="c1", output="{}"),
    )
    assert item.data.call_id == "c1"


def test_new_item_reasoning() -> None:
    item = NewConversationItem(
        type="reasoning",
        response_id="resp_1",
        data=ReasoningData(agent="my-agent", summary=[]),
    )
    assert item.type == "reasoning"


def test_new_item_type_data_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="requires FunctionCallData, got MessageData"):
        NewConversationItem(
            type="function_call",
            response_id="resp_1",
            data=MessageData(role="user", content=[]),
        )


def test_new_item_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown item type"):
        NewConversationItem(
            type="unknown",
            response_id="resp_1",
            data=MessageData(role="user", content=[]),
        )


# ── ConversationItem ───────────────────────────────────


def test_conversation_item_valid() -> None:
    item = ConversationItem(
        id="item_1",
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=1700000000,
        data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
    )
    assert item.id == "item_1"
    assert item.status == "completed"
    assert item.created_at == 1700000000


def test_conversation_item_type_data_mismatch_rejected() -> None:
    with pytest.raises(ValidationError):
        ConversationItem(
            id="item_1",
            type="reasoning",
            status="completed",
            response_id="resp_1",
            created_at=1700000000,
            data=MessageData(role="user", content=[]),
        )


# ── parse_item_data ────────────────────────────────────


def test_parse_message() -> None:
    data = parse_item_data("message", {"role": "user", "content": []})
    assert isinstance(data, MessageData)
    assert data.role == "user"


def test_parse_meta_message() -> None:
    """Persisted JSON rows with ``is_meta`` hydrate as hidden messages."""
    data = parse_item_data("message", {"role": "user", "content": [], "is_meta": True})
    assert isinstance(data, MessageData)
    assert data.is_meta is True


def test_parse_function_call() -> None:
    data = parse_item_data(
        "function_call",
        {"agent": "a", "name": "fn", "arguments": "{}", "call_id": "c1"},
    )
    assert isinstance(data, FunctionCallData)


def test_parse_function_call_output() -> None:
    data = parse_item_data("function_call_output", {"call_id": "c1", "output": "{}"})
    assert isinstance(data, FunctionCallOutputData)


def test_parse_reasoning() -> None:
    data = parse_item_data("reasoning", {"agent": "a", "summary": []})
    assert isinstance(data, ReasoningData)


def test_parse_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown item type"):
        parse_item_data("bogus", {})


def test_parse_invalid_data() -> None:
    with pytest.raises(ValidationError):
        parse_item_data("function_call", {"agent": "a"})


# ── SlashCommandData ───────────────────────────────────


def test_parse_slash_command_minimal() -> None:
    """Skill record (no args, no stdout) round-trips with ``output=None``."""
    data = parse_item_data(
        "slash_command",
        {"agent": "claude-native-ui", "name": "dev-productivity:simplify", "arguments": ""},
    )
    assert isinstance(data, SlashCommandData)
    assert data.name == "dev-productivity:simplify"
    assert data.arguments == ""
    assert data.output is None
    # Records persisted before the ``kind`` field was added default to
    # ``"skill"`` so old data deserializes without backfill.
    assert data.kind == "skill"


def test_parse_slash_command_with_stdout() -> None:
    """Record with inline stdout deserializes intact."""
    data = parse_item_data(
        "slash_command",
        {
            "agent": "claude-native-ui",
            "name": "oncall",
            "arguments": "file-bug",
            "output": "oncall: file-bug subcommand started",
        },
    )
    assert isinstance(data, SlashCommandData)
    assert data.output == "oncall: file-bug subcommand started"


def test_parse_slash_command_kind_command_for_surfaced_cli() -> None:
    """``kind="command"`` round-trips so the UI can switch the prefix."""
    data = parse_item_data(
        "slash_command",
        {
            "agent": "claude-native-ui",
            "kind": "command",
            "name": "effort",
            "arguments": "high",
        },
    )
    assert isinstance(data, SlashCommandData)
    assert data.kind == "command"


def test_slash_command_serializes_agent_as_model() -> None:
    """``agent`` aliases to ``model`` on the wire (parity with MessageData)."""
    data = SlashCommandData(
        agent="claude-native-ui",
        name="dev-productivity:simplify",
        arguments="",
    )
    dumped = data.model_dump(exclude_none=True, by_alias=True)
    # ``kind`` defaults to ``"skill"`` and is included on the wire so
    # the UI gets a single shape to switch on (no implicit fallback).
    assert dumped == {
        "model": "claude-native-ui",
        "kind": "skill",
        "name": "dev-productivity:simplify",
        "arguments": "",
    }


def test_slash_command_is_non_content() -> None:
    """Contract guard: removing this entry leaks items into LLM prompts."""
    assert "slash_command" in NON_CONTENT_ITEM_TYPES


# ── Conversation ──────────────────────────────────────


def test_conversation_title_defaults_to_none() -> None:
    """Conversation created without a title has title=None and
    empty labels; timestamps reflect what was passed in."""
    conv = Conversation(
        id="conv_1",
        created_at=1700000000,
        updated_at=1700000000,
        root_conversation_id="conv_1",
    )
    assert conv.title is None
    # Labels default to empty dict, not None — iteration via
    # ``.items()`` must always be safe.
    assert conv.labels == {}


def test_conversation_title_set() -> None:
    """Title is persisted when passed at construction."""
    conv = Conversation(
        id="conv_1",
        created_at=1700000000,
        updated_at=1700000000,
        root_conversation_id="conv_1",
        title="Weather chat",
    )
    assert conv.title == "Weather chat"


def test_conversation_labels_default_independent() -> None:
    """Each Conversation gets its own labels dict via
    ``default_factory``. If this test fails, two Conversations
    would share state (the classic mutable-default footgun)."""
    a = Conversation(id="conv_a", created_at=1, updated_at=1, root_conversation_id="conv_a")
    b = Conversation(id="conv_b", created_at=1, updated_at=1, root_conversation_id="conv_b")
    a.labels["integrity"] = "0"
    # Writing on a must NOT show up on b.
    assert b.labels == {}
