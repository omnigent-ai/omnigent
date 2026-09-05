from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.routes._sessions.helpers import _merge_pending_file_blocks


def test_native_pending_merge_retains_inline_attachment_position() -> None:
    item = NewConversationItem(
        type="message",
        response_id="resp_1",
        data=MessageData(role="user", content=[{"type": "input_text", "text": "before after"}]),
    )
    pending = [
        {"type": "input_text", "text": "before "},
        {"type": "input_image", "file_id": "file_1", "filename": "middle.png"},
        {"type": "input_text", "text": "after"},
    ]

    merged = _merge_pending_file_blocks(item, pending)

    assert isinstance(merged.data, MessageData)
    assert merged.data.content == pending
