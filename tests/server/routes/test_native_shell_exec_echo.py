"""A ``!`` shell exec's mirrored user echo settles only its own queued web input."""

import json
from collections.abc import Iterator
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

_WEB_MESSAGE = [{"type": "input_text", "text": "please review the diff"}]
_ATTACHMENT = {"type": "input_file", "file_id": "file_diff", "filename": "diff.patch"}
_BANG = [{"type": "input_text", "text": "! pwd"}]
_FORWARDER = "forwarder@example.com"


@pytest.fixture(autouse=True)
def _isolated_pending_inputs() -> Iterator[None]:
    pending_inputs.reset_for_tests()
    yield
    pending_inputs.reset_for_tests()


def _bang_echo_body(tmp_path: Path) -> SessionEventInput:
    """Mirror Claude's ``<bash-input>`` record for ``! pwd`` into the echo's event body."""
    transcript = tmp_path / "session.jsonl"
    record = {
        "type": "user",
        "uuid": "bash-in",
        "message": {"role": "user", "content": "<bash-input> pwd</bash-input>"},
    }
    transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
    echo, _input_card = read_transcript_items_since(transcript, 0, agent_name="Claude")[2]
    assert echo.shell_command_echo
    return SessionEventInput.model_validate(_external_conversation_item_event(echo))


def _web_mirror_body(content: list[dict[str, Any]]) -> SessionEventInput:
    """A plain user message mirrored back from the transcript."""
    return SessionEventInput(
        type="external_conversation_item",
        data={
            "source_id": "web-1:0:message",
            "item_type": "message",
            "item_data": {"role": "user", "content": content},
            "response_id": "resp_claude_web",
        },
    )


def _cleared_pending_ids(publish: Mock) -> list[str | None]:
    return [
        call.args[1]["data"]["cleared_pending_id"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]


def _consumed_user_authored(publish: Mock) -> list[object]:
    return [
        call.args[1]["data"]["data"].get("user_authored")
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]


def _consumed_shell_command_echo(publish: Mock) -> list[object]:
    return [
        call.args[1]["data"]["shell_command_echo"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]


def _message_by_text(store: SqlAlchemyConversationStore, session_id: str, text: str) -> Any:
    [item] = [
        item
        for item in store.list_items(session_id, type="message").data
        if item.data.content[-1]["text"] == text
    ]
    return item


@pytest.mark.asyncio
async def test_web_bang_echo_drains_only_its_own_queued_entry(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The echo matches the composer's queued text and leaves older entries queued."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Review")
    older = pending_inputs.record(conv.id, _WEB_MESSAGE, created_by="alice@example.com")
    bang = pending_inputs.record(conv.id, _BANG, created_by="carol@example.com")
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    await _persist_external_conversation_item(
        conv.id, conv, _bang_echo_body(tmp_path), store, created_by=_FORWARDER
    )

    echo = _message_by_text(store, conv.id, "! pwd")
    assert echo.created_by == "carol@example.com"
    # Flagged human-authored so it renders as a user bubble on reload.
    assert echo.data.user_authored is True
    assert _cleared_pending_ids(publish) == [bang]
    # It matched a composer send but is still a shell exec; the consumed
    # event flags it so the client never pops it off the FIFO by position.
    assert _consumed_shell_command_echo(publish) == [True]
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [older]


@pytest.mark.asyncio
async def test_terminal_bang_echo_leaves_unrelated_queued_web_message_alone(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bang typed in the terminal queued nothing, so its echo must not drain by position.

    Draining the oldest entry would hand the web message's author (and any
    attachments) to the bang and leave that message with no entry to settle
    when its own mirror arrives.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Review")
    web = pending_inputs.record(
        conv.id, [_ATTACHMENT, *_WEB_MESSAGE], created_by="alice@example.com"
    )
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)

    await _persist_external_conversation_item(
        conv.id, conv, _bang_echo_body(tmp_path), store, created_by=_FORWARDER
    )

    echo = _message_by_text(store, conv.id, "! pwd")
    assert echo.created_by == _FORWARDER
    # The consumed event's explicit shell_command_echo flag — not authorship —
    # tells the web client this terminal-typed exec owns no optimistic bubble,
    # so it must not pop the unrelated queued web message off the FIFO head.
    assert echo.data.user_authored is True
    assert _cleared_pending_ids(publish) == [None]
    assert _consumed_user_authored(publish) == [True]
    assert _consumed_shell_command_echo(publish) == [True]
    # The web message (and its attachment) stays queued for its own mirror.
    [retained] = pending_inputs.snapshot_for(conv.id)
    assert retained["pending_id"] == web
    assert _ATTACHMENT in retained["content"]

    await _persist_external_conversation_item(
        conv.id, conv, _web_mirror_body(_WEB_MESSAGE), store, created_by=_FORWARDER
    )

    mirrored = _message_by_text(store, conv.id, "please review the diff")
    assert mirrored.created_by == "alice@example.com"
    # The text-only mirror re-adopts the queued entry's attachment block.
    assert any(block.get("type") == "input_file" for block in mirrored.data.content)
    assert _cleared_pending_ids(publish) == [None, web]
    assert _consumed_shell_command_echo(publish) == [True, False]
    assert pending_inputs.snapshot_for(conv.id) == []
