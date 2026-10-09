"""Tests for externally-forwarded conversation-item parsing."""

import pytest

from omnigent.entities import ConversationItem, MessageData
from omnigent.server.routes._sessions.helpers import _build_new_item
from omnigent.server.routes.sessions import _parse_external_conversation_item
from omnigent.server.schemas import SessionEventInput


@pytest.mark.parametrize("response_id", [None, "external-record", "turn_external"])
@pytest.mark.parametrize("is_meta", [False, True])
def test_mirrored_user_messages_are_durably_marked_as_history_only(
    response_id: str | None, is_meta: bool
) -> None:
    parsed = _parse_external_conversation_item(
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Already handled elsewhere."}],
                    "is_meta": is_meta,
                    "history_only": False,
                },
                "response_id": response_id,
            },
        )
    )
    persisted = ConversationItem(
        id="mirror",
        type=parsed.type,
        status="completed",
        response_id=parsed.response_id,
        created_at=1,
        data=parsed.data,
    )
    assert persisted.to_api_dict().get("history_only") is True


@pytest.mark.parametrize("is_meta", [False, True])
def test_posted_inputs_cannot_opt_out_of_reconnect_recovery(is_meta: bool) -> None:
    item = _build_new_item(
        SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "Run this input."}],
                "is_meta": is_meta,
                "history_only": True,
            },
        ),
        "turn_input",
    )
    assert isinstance(item.data, MessageData)
    assert item.data.history_only is False


def test_native_message_stream_id_survives_snapshot_serialization() -> None:
    """The completed item durably identifies the preview stream it replaces."""
    parsed = _parse_external_conversation_item(
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                "response_id": "turn_1",
                "message_id": "codex:thread_1:turn_1:agentMessage:item_1",
            },
        )
    )

    assert isinstance(parsed.data, MessageData)
    assert parsed.data.stream_message_id == "codex:thread_1:turn_1:agentMessage:item_1"
    persisted = ConversationItem(
        id="item_1",
        type=parsed.type,
        status="completed",
        response_id=parsed.response_id,
        created_at=1,
        data=parsed.data,
    )
    assert persisted.to_api_dict()["stream_message_id"] == (
        "codex:thread_1:turn_1:agentMessage:item_1"
    )
