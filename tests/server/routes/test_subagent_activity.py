"""Lifecycle notices remain named, ordered, and idempotent across native delivery races."""

from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _persist_external_conversation_items,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.server.subagent_activity import _title, record_subagent_activity
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize("explicit_turn", [False, True])
@pytest.mark.asyncio
async def test_lifecycle_publishes_once_per_child_turn(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, explicit_turn: bool
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="researcher:Audit")
    publish = Mock()
    monkeypatch.setattr("omnigent.server.subagent_activity.session_stream.publish", publish)
    await record_subagent_activity(child.id, "delegated", store, parent_id="wrong-parent")
    await record_subagent_activity(child.id, "returned", store)
    assert store.list_items(parent.id).data == []
    for _ in range(2):
        await record_subagent_activity(child.id, "delegated", store)
    for turn_id in ("first", "second"):
        store.append(
            child.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=turn_id,
                    data=MessageData(
                        role="assistant",
                        agent="Claude",
                        content=[{"type": "output_text", "text": "Done"}],
                    ),
                )
            ],
        )
        for _ in range(2):
            await record_subagent_activity(
                child.id, "returned", store, turn_id=turn_id if explicit_turn else None
            )
    items = store.list_items(parent.id).data
    assert [item.data.event_type for item in items] == [
        "session.subagent.delegated",
        "session.subagent.returned",
        "session.subagent.returned",
    ]
    assert publish.call_count == 3
    for call, item in zip(publish.call_args_list, items, strict=True):
        assert call.args[0] == parent.id
        assert call.args[1]["type"] == "response.output_item.done"
        assert call.args[1]["item"]["id"] == item.id
        assert item.data.resource_id == child.id
        assert item.data.resource == {"title": "Audit"}


def _message(text: str) -> dict[str, Any]:
    return {"role": "user", "is_meta": True, "content": [{"type": "input_text", "text": text}]}


@pytest.mark.parametrize(
    "late,batched", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize(
    "data,return_id,expected",
    [
        ({"call_id": "tool-1", "output": "Done"}, "agent-1", "completed"),
        (_message('<agent-message from="reviewer">Done</agent-message>'), "agent-1", "completed"),
        (
            _message(
                "<task-notification><task-id>agent-1</task-id><status>completed</status>"
                "</task-notification>"
            ),
            None,
            "completed",
        ),
        (
            _message(
                "<task-notification><tool-use-id>tool-1</tool-use-id><status>failed</status>"
                "</task-notification>"
            ),
            None,
            "failed",
        ),
        (
            {"call_id": "tool-1", "output": "Async agent launched successfully. agentId: agent-1"},
            None,
            None,
        ),
        ({"call_id": "tool-1", "output": '{"status":"failed","agentId":"agent-1"}'}, None, None),
        (_message('<teammate-message teammate_id="reviewer">Hi</teammate-message>'), None, None),
    ],
    ids=["tool", "handback", "notification", "failure", "launch", "unconfirmed", "chatter"],
)
@pytest.mark.asyncio
async def test_claude_completion_survives_retries_and_late_child_discovery(
    db_uri: str,
    data: dict[str, Any],
    return_id: str | None,
    expected: str | None,
    late: bool,
    batched: bool,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()

    async def register_child() -> str:
        child = store.create_conversation(
            parent_conversation_id=parent.id, title="Explore:agent-1"
        )
        store.set_labels(
            child.id,
            {
                "omnigent.claude_native.subagent_id": "agent-1",
                "omnigent.claude_native.tool_use_id": "tool-1",
                "omnigent.claude_native.description": "Inspect authentication",
            },
        )
        await record_subagent_activity(child.id, "delegated", store)
        return child.id

    child_id = None if late else await register_child()
    item_type = "function_call_output" if "call_id" in data else "message"
    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "source_id": "result",
            "response_id": "parent-turn",
            "item_type": item_type,
            "item_data": data,
            "subagent_return_id": return_id,
        },
    )

    async def deliver() -> None:
        if batched:
            await _persist_external_conversation_items(parent.id, [body], store)
        else:
            await _persist_external_conversation_item(parent.id, parent, body, store)

    if late and expected:

        def fail_marker_write(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT") and "session.subagent.completion-observed" in str(
                parameters
            ):
                raise RuntimeError("marker write failed")

        event.listen(store._conv_engine, "after_cursor_execute", fail_marker_write)
        try:
            with pytest.raises(RuntimeError, match="marker write failed"):
                await deliver()
        finally:
            event.remove(store._conv_engine, "after_cursor_execute", fail_marker_write)
        assert store.list_items(parent.id).data == []
    for _ in range(2):
        await deliver()
    [persisted] = store.list_items(parent.id, type=item_type).data
    assert persisted.data.subagent_return_id == return_id
    if late:
        # Reconciliation must work even when completion is outside the latest history page.
        store.append(
            parent.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"update-{i}",
                    data=MessageData(
                        role="assistant",
                        agent="Claude",
                        content=[{"type": "output_text", "text": "Update"}],
                    ),
                )
                for i in range(101)
            ],
        )
        child_id = await register_child()
    assert child_id is not None
    await record_subagent_activity(child_id, "delegated", store)
    activity = [
        row
        for row in store.list_items(parent.id, type="resource_event").data
        if row.data.event_type != "session.subagent.completion-observed"
    ]
    assert [row.data.event_type for row in activity] == ["session.subagent.delegated"] + (
        ["session.subagent.returned"] if expected else []
    )
    assert all(row.data.resource_id == child_id for row in activity)
    assert activity[-1].data.resource == {
        "title": "Inspect authentication",
        **({"status": expected} if expected else {}),
    }


def test_claude_child_title_prefers_the_agent_name(db_uri: str) -> None:
    """A named teammate is announced by the name the user addresses it with."""
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="buddy:abuddy-1")
    store.set_labels(
        child.id,
        {
            "omnigent.wrapper": "claude-code-native-ui-subagent",
            "omnigent.claude_native.subagent_name": "buddy",
            "omnigent.claude_native.description": "Probe teammate",
        },
    )
    named = store.get_conversation(child.id)
    assert named is not None
    assert _title(named) == "buddy"
