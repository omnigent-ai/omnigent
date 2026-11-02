"""Native provenance distinguishes internal deliveries from human-authored agent markup."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native.bridge import read_transcript_items_since
from omnigent.harnesses.claude_native.forwarder import _external_conversation_item_event
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _settle_undelivered_native_input,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_ENVELOPE = '<teammate-message teammate_id="reviewer">Review this</teammate-message>'
# Completion emitted by a named Claude Agent Teams worker.
_TEAM_COMPLETION = """<task-notification>
<task-id>ae6a7749a5dc6041c</task-id><tool-use-id>toolu_agent_team</tool-use-id>
<status>completed</status><summary>Agent "Message probe" finished</summary>
<result>TEAM_DONE</result></task-notification>"""


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
    ],
    ids=["web-teammate", "web-handback", "terminal", "internal-handback", "agent-team"],
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


_PARENT_SOURCE = {"source_session_id": "conv_parent", "source_agent_name": "Claude"}


@pytest.mark.parametrize(
    "queued_text,message_source,applied",
    [
        ("Do the task", None, False),
        ("Do the task", _PARENT_SOURCE, True),
        ("Agent task", _PARENT_SOURCE, False),
    ],
    ids=["typed", "agent-sent", "terminal-typed-over-agent-sent"],
)
@pytest.mark.asyncio
async def test_native_drain_applies_only_the_queued_message_source(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    queued_text: str,
    message_source: dict[str, str] | None,
    applied: bool,
) -> None:
    """Only the text-matched pending entry, never the forwarder, decides who sent a message."""
    content = [{"type": "input_text", "text": "Do the task"}]
    forged = {"source_session_id": "conv_forged", "source_agent_name": "Mallory"}
    body = SessionEventInput.model_validate(
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {"role": "user", "content": content, **forged},
                "response_id": "resp_native",
            },
        }
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Child")
    pending_inputs.record(
        conv.id, [{"type": "input_text", "text": queued_text}], message_source=message_source
    )
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    await _persist_external_conversation_item(conv.id, conv, body, store)

    assert pending_inputs.snapshot_for(conv.id) == []
    [item] = store.list_items(conv.id, type="message").data
    unlabeled = {"source_session_id": None, "source_agent_name": None}
    expected = message_source if applied and message_source else unlabeled
    assert item.data.source_session_id == expected["source_session_id"]
    assert item.data.source_agent_name == expected["source_agent_name"]
    [receipt] = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    assert receipt["data"].get("source_session_id") == expected["source_session_id"]
    assert receipt["data"].get("source_agent_name") == expected["source_agent_name"]


@pytest.mark.parametrize("failure", ["reported_undelivered", "skipped_by_later_mirror"])
@pytest.mark.asyncio
async def test_native_failure_paths_keep_the_queued_message_source(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A parent's message the harness never recorded still names its sender."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Child")
    lost = [{"type": "input_text", "text": "Agent task"}]
    stable_id = "7f3a9c1e5b2d4f6a8c0e1d2b3a4f5c6d"
    pending_inputs.record(conv.id, lost, stable_id=stable_id, message_source=_PARENT_SOURCE)
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    if failure == "reported_undelivered":
        await _settle_undelivered_native_input(store, conv.id, "resp_failed", stable_id)
    else:
        typed = [{"type": "input_text", "text": "Do the task"}]
        pending_inputs.record(conv.id, typed)
        body = SessionEventInput.model_validate(
            {
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {"role": "user", "content": typed},
                    "response_id": "resp_native",
                    "source_id": "claude:typed:0",
                },
            }
        )
        await _persist_external_conversation_item(conv.id, conv, body, store)
        [error] = store.list_items(conv.id, type="error").data
        assert error.data.code == "native_prompt_not_recorded"

    assert pending_inputs.snapshot_for(conv.id) == []
    messages = store.list_items(conv.id, type="message").data
    sources = {
        message.data.content[0]["text"]: (
            message.data.source_session_id,
            message.data.source_agent_name,
        )
        for message in messages
    }
    assert sources.pop("Agent task") == ("conv_parent", "Claude")
    assert set(sources.values()) <= {(None, None)}
    [receipt] = [
        call.args[1]["data"]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
        and call.args[1]["data"]["data"]["content"] == lost
    ]
    assert {key: receipt.get(key) for key in _PARENT_SOURCE} == _PARENT_SOURCE
