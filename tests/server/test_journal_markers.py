"""Chat markers for a session's tasks: what each plan change and to-do update writes."""

from __future__ import annotations

import pytest

from omnigent.server.journal_markers import (
    PLAN_EVENT_TYPE,
    TASK_EVENT_TYPE,
    plan_change,
    record_todo_markers,
    todo_transitions,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _step(step_id: str, name: str | None = None) -> dict[str, object]:
    return {"id": step_id, "name": name or step_id.upper(), "goal": None, "depends_on": []}


def _todo(content: str, status: str, todo_id: str | None = None) -> dict[str, str]:
    todo = {"content": content, "status": status, "activeForm": content}
    return {**todo, "id": todo_id} if todo_id else todo


def test_a_plan_change_names_what_it_added_and_removed_by_task_id() -> None:
    first = {"name": "Ship", "steps": [_step("a"), _step("b")]}
    assert plan_change(None, first) == {
        "change": "made",
        "name": "Ship",
        "tasks": 2,
        "added": [],
        "removed": [],
    }
    assert plan_change(first, first) is None
    assert plan_change(first, {**first, "name": "Ship it"})["change"] == "renamed"
    replan = plan_change(first, {"name": "Ship", "steps": [_step("a"), _step("c")]})
    assert (replan["change"], replan["added"], replan["removed"]) == ("updated", ["C"], ["B"])
    # Renaming or reordering tasks changes the plan without adding or removing any.
    for steps in ([_step("b"), _step("a")], [_step("a", "Renamed"), _step("b")]):
        change = plan_change(first, {"name": "Ship", "steps": steps})
        assert (change["change"], change["added"], change["removed"]) == ("updated", [], [])
    replaced = plan_change(first, {"name": "Fix the test", "steps": [_step("x")]})
    assert (replaced["change"], replaced["name"], replaced["tasks"]) == (
        "replaced",
        "Fix the test",
        1,
    )


def test_a_to_do_marks_starting_and_completing_matched_by_id_or_position() -> None:
    before = [_todo("Add the flag", "in_progress", "1"), _todo("Document it", "pending", "2")]
    # Claude's IDs keep a renamed, reordered task the same task.
    after = [
        _todo("Document it", "in_progress", "2"),
        _todo("Add the --json flag", "completed", "1"),
    ]
    assert [(key, status) for key, _, status in todo_transitions(before, after)] == [
        ("2", "started"),
        ("1", "done"),
    ]
    assert todo_transitions(after, after) == []
    # Without IDs (Codex's plan), tasks match by position.
    assert [
        (key, status)
        for key, _, status in todo_transitions(
            [_todo("A", "pending")], [_todo("A", "in_progress")]
        )
    ] == [("task-1", "started")]
    # A task that arrives already in progress starts there.
    assert [key for key, _, _ in todo_transitions([], [_todo("A", "in_progress", "9")])] == ["9"]


@pytest.mark.asyncio
async def test_a_retried_to_do_update_adds_its_markers_once(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation().id
    before = [_todo("Add the flag", "pending", "1")]
    after = [_todo("Add the flag", "in_progress", "1")]
    for _ in range(2):
        await record_todo_markers(store, session_id, before, after)
    items = store.list_items(session_id, limit=10).data
    assert [
        (item.data.event_type, item.data.resource_id, item.data.resource) for item in items
    ] == [
        (
            TASK_EVENT_TYPE,
            "1",
            {
                "name": "Add the flag",
                "status": "started",
                "text": None,
                "entry_id": None,
                "native": True,
            },
        )
    ]
    assert PLAN_EVENT_TYPE != TASK_EVENT_TYPE
