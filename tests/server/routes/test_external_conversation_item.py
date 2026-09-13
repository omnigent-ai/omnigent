"""Tests for externally-forwarded conversation-item parsing."""

from omnigent.entities import ConversationItem, MessageData
from omnigent.server.routes.sessions import _parse_external_conversation_item
from omnigent.server.schemas import SessionEventInput


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
