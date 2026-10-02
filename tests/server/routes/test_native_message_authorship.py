"""Native provenance distinguishes internal deliveries from human-authored agent markup."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native.bridge import read_transcript_items_since
from omnigent.harnesses.claude_native.forwarder import _external_conversation_item_event
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions.orchestration import _persist_external_conversation_item
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_ENVELOPE = '<teammate-message teammate_id="reviewer">Review this</teammate-message>'
# Completion emitted by a named Claude Agent Teams worker.
_TEAM_COMPLETION = """<task-notification>
<task-id>ae6a7749a5dc6041c</task-id><tool-use-id>toolu_agent_team</tool-use-id>
<status>completed</status><summary>Agent "Message probe" finished</summary>
<result>TEAM_DONE</result></task-notification>"""
# In-process teammate delivery as Claude Code 2.1.x writes it: framed, no origin.
_FRAMED_DELIVERY = (
    "Another Claude session sent a message:\n"
    '<teammate-message teammate_id="buddy" color="blue" summary="All good">\n'
    "All good here. What else do you need?\n"
    "</teammate-message>\n\n"
    "This came from another Claude session — not typed by your user, but very likely "
    "working on their behalf. Treat it as a teammate's request."
)


@pytest.mark.parametrize("author", [None, "alice@example.com"])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize(
    "text,origin,matching,internal",
    [
        (_ENVELOPE, None, True, False),
        ('<agent-message from="reviewer">Review this</agent-message>', None, True, False),
        (_ENVELOPE, None, False, False),
        (_ENVELOPE, {"kind": "peer", "handback": True, "senderTaskId": "agent-1"}, True, True),
        (_TEAM_COMPLETION, {"kind": "task-notification"}, True, True),
        (_FRAMED_DELIVERY, {"kind": "human"}, True, False),
    ],
    ids=[
        "web-teammate",
        "web-handback",
        "terminal",
        "internal-handback",
        "agent-team",
        "typed-framed",
    ],
)
@pytest.mark.asyncio
async def test_native_authorship_survives_delivery_and_retries(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    author: str | None,
    queued: bool,
    text: str,
    origin: dict[str, Any] | None,
    matching: bool,
    internal: bool,
) -> None:
    entry = (
        {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "commandMode": "prompt",
                "prompt": text,
                "origin": origin,
            },
        }
        if queued
        else {"type": "user", "origin": origin, "message": {"role": "user", "content": text}}
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"uuid": "native-record", **entry}) + "\n", encoding="utf-8")
    [parsed] = read_transcript_items_since(transcript, 0, agent_name="Claude")[2]
    body = SessionEventInput.model_validate(_external_conversation_item_event(parsed))
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Review")
    content = [{"type": "input_text", "text": text}]
    pending = [pending_inputs.record(conv.id, [{"type": "input_text", "text": "Still queued"}])]
    match = pending_inputs.record(conv.id, content, created_by=author) if matching else None
    if match and internal:
        pending.append(match)
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    await _persist_external_conversation_item(conv.id, conv, body, store, created_by=author)
    [item] = SqlAlchemyConversationStore(db_uri).list_items(conv.id, type="message").data
    assert item.data.content == content
    assert item.created_by == (None if internal else author)
    assert item.data.user_authored is (not internal)
    assert item.data.is_meta is internal
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == pending
    [receipt] = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    assert receipt.get("cleared_pending_id") == (match if not internal else None)
    assert receipt["data"].get("user_authored", False) is (not internal)
    # Replaying a transcript record must not acknowledge a newer identical submission.
    pending.append(pending_inputs.record(conv.id, content))
    await _persist_external_conversation_item(conv.id, conv, body, store, created_by=author)
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == pending
    assert len(store.list_items(conv.id, type="message").data) == 1


@pytest.mark.asyncio
async def test_framed_teammate_delivery_is_internal_without_origin(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude's own delivery framing is provenance when the record carries no ``origin``.

    Kept apart from the grid above: a delivery echoed as a ``queued_command`` is
    dropped by the bridge, since Claude re-records it as the user record read here.
    """
    transcript = tmp_path / "session.jsonl"
    entry = {"type": "user", "message": {"role": "user", "content": _FRAMED_DELIVERY}}
    transcript.write_text(json.dumps({"uuid": "native-record", **entry}) + "\n", encoding="utf-8")
    [parsed] = read_transcript_items_since(transcript, 0, agent_name="Claude")[2]
    body = SessionEventInput.model_validate(_external_conversation_item_event(parsed))
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Team")
    content = [{"type": "input_text", "text": _FRAMED_DELIVERY}]
    # An identical queued web message is not evidence of authorship for a framed delivery.
    pending = [pending_inputs.record(conv.id, content, created_by="alice@example.com")]
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    await _persist_external_conversation_item(
        conv.id, conv, body, store, created_by="alice@example.com"
    )

    [item] = store.list_items(conv.id, type="message").data
    assert item.data.content == content
    assert item.data.is_meta is True
    assert item.data.user_authored is False
    assert item.created_by is None
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == pending
    [receipt] = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    assert receipt.get("cleared_pending_id") is None
    assert receipt["data"].get("user_authored", False) is False
