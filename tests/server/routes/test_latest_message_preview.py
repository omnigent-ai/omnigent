"""Sub-agent rail preview text built from a child's newest message."""

from omnigent.entities.conversation import ConversationItem, MessageData
from omnigent.server.routes._sessions.helpers import _latest_message_preview


def _message(role: str, block_type: str, text: str) -> ConversationItem:
    return ConversationItem(
        id="item_1",
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=0,
        data=MessageData(
            role=role,
            agent="researcher" if role == "assistant" else None,
            content=[{"type": block_type, "text": text}],
        ),
    )


def test_preview_reads_inline_mentions_as_names() -> None:
    text = (
        'compare [Session: "Fix CI" id=s_1 subagent=no] with that run\n\n'
        "[Tagged sessions: read with sys_session_get_history]"
    )
    assert _latest_message_preview([_message("user", "input_text", text)]) == (
        "compare @Fix CI with that run"
    )


def test_preview_skips_a_message_that_is_only_the_session_hint() -> None:
    items = [
        _message("user", "input_text", "[Tagged sessions: read with sys_session_get_history]"),
        _message("assistant", "output_text", "done"),
    ]
    assert _latest_message_preview(items) == "done"
