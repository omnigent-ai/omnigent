"""Chat markers for a session's tasks: where each task starts and ends, and each plan change.

The journal route and the to-do path write them into the session's transcript as
``resource_event`` items, the way sub-agent markers are written, so they're
saved, stream live and keep their place in the chat. Each has a stable ID, so a
retried write adds one marker. They're display metadata: a failure to write one
is logged and never fails the write that caused it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.entities import NewConversationItem, ResourceEventData
from omnigent.entities.session_journal import JournalEntry
from omnigent.runtime import session_stream
from omnigent.server.schemas import OutputItemDoneEvent
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

#: ``event_type`` of a task's marker: it started, finished, failed or waits for the user.
TASK_EVENT_TYPE = "session.journal.task"
#: ``event_type`` of a plan-change line.
PLAN_EVENT_TYPE = "session.journal.plan"
RESOURCE_TYPE = "journal"

# A to-do's status, as the marker for reaching it names it.
_TODO_MARKER_STATUS = {"in_progress": "started", "completed": "done"}


def plan_change(
    previous: Mapping[str, Any] | None, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """How *plan* changed *previous*, compared by step ID; ``None`` when nothing changed.

    :param previous: The previous plan's payload, or ``None`` for the first plan.
    :param plan: The new plan's payload, e.g. ``{"name": "Ship it", "steps": [...]}``.
    :returns: ``{"change", "name", "tasks", "added", "removed"}``, where change is
        ``made`` (the first plan), ``replaced`` (no task carries over), ``renamed``
        (only the name changed) or ``updated``; added and removed list task names.
    """
    steps = list(plan.get("steps") or [])
    name = plan.get("name")
    change: dict[str, Any] = {"name": name, "tasks": len(steps), "added": [], "removed": []}
    if previous is None:
        return {**change, "change": "made"}
    before = {step["id"]: step for step in previous.get("steps") or []}
    after = {step["id"]: step for step in steps}
    if not before.keys() & after.keys():
        return {**change, "change": "replaced"}
    change["added"] = [step["name"] for step in steps if step["id"] not in before]
    change["removed"] = [
        step["name"] for step in previous.get("steps") or [] if step["id"] not in after
    ]
    if change["added"] or change["removed"]:
        return {**change, "change": "updated"}
    if list(previous.get("steps") or []) != steps:
        return {**change, "change": "updated"}  # renamed, reordered or rewired tasks
    if previous.get("name") != name:
        return {**change, "change": "renamed"}
    return None


def todo_transitions(
    previous: Sequence[Mapping[str, Any]], todos: Sequence[Mapping[str, Any]]
) -> list[tuple[str, Mapping[str, Any], str]]:
    """The to-dos that just became in progress or completed, as ``(key, todo, status)``.

    A to-do is matched to its earlier self by ``id`` when both carry one (Claude's
    task IDs), otherwise by position (Codex's plan has no IDs). Its key is that ID,
    or ``task-<n>`` by position, as the Tasks tab names it.

    :returns: One entry per marker to write; status is ``started`` or ``done``.
    """
    earlier = {todo["id"]: todo for todo in previous if todo.get("id")}
    found = []
    for index, todo in enumerate(todos):
        status = _TODO_MARKER_STATUS.get(str(todo.get("status")))
        if status is None:
            continue
        key = str(todo.get("id") or f"task-{index + 1}")
        if todo.get("id"):
            before = earlier.get(todo["id"])
        else:
            before = previous[index] if index < len(previous) else None
        if before is not None and before.get("status") == todo.get("status"):
            continue
        found.append((key, todo, status))
    return found


async def record_status_marker(
    store: ConversationStore, session_id: str, entry: JournalEntry, plan: JournalEntry | None
) -> None:
    """Mark where a plan task started, finished, failed or began waiting for the user.

    :param entry: The status entry the journal accepted.
    :param plan: The plan it was written against, which names the task.
    """
    names = {step["id"]: step["name"] for step in (plan.payload["steps"] if plan else [])}
    step = entry.step or ""
    await _append(
        store,
        session_id,
        f"journal:{entry.id}",
        ResourceEventData(
            event_type=TASK_EVENT_TYPE,
            resource_id=step,
            resource_type=RESOURCE_TYPE,
            resource={
                "name": names.get(step, step),
                "status": entry.payload["status"],
                "text": entry.payload.get("text"),
                "entry_id": entry.id,
                "native": False,
            },
        ),
    )


async def record_plan_marker(
    store: ConversationStore, session_id: str, entry: JournalEntry, previous: JournalEntry | None
) -> None:
    """Write the line for a plan the journal accepted, unless it changes nothing.

    :param previous: The plan before it, or ``None`` for the session's first.
    """
    change = plan_change(previous.payload if previous else None, entry.payload)
    if change is None:
        return
    await _append(
        store,
        session_id,
        f"journal:{entry.id}",
        ResourceEventData(
            event_type=PLAN_EVENT_TYPE,
            resource_id=entry.id,
            resource_type=RESOURCE_TYPE,
            resource={**change, "entry_id": entry.id},
        ),
    )


async def record_todo_markers(
    store: ConversationStore,
    session_id: str,
    previous: Sequence[Mapping[str, Any]],
    todos: Sequence[Mapping[str, Any]],
) -> None:
    """Mark each of the agent's own tasks that started or completed since *previous*."""
    # Keyed on the snapshot it moved from, so a retried identical post adds nothing.
    basis = hashlib.sha256(json.dumps(list(previous), sort_keys=True).encode()).hexdigest()
    for key, todo, status in todo_transitions(previous, todos):
        await _append(
            store,
            session_id,
            f"todo:{key}:{status}:{basis}",
            ResourceEventData(
                event_type=TASK_EVENT_TYPE,
                resource_id=key,
                resource_type=RESOURCE_TYPE,
                resource={
                    "name": todo.get("content") or key,
                    "status": status,
                    "text": None,
                    "entry_id": None,
                    "native": True,
                },
            ),
        )


async def _append(
    store: ConversationStore, session_id: str, key: str, data: ResourceEventData
) -> None:
    stable_id = hashlib.sha256(f"{session_id}:{key}".encode()).hexdigest()[:32]
    item = NewConversationItem(
        type="resource_event",
        stable_id=stable_id,
        response_id="journal_" + stable_id,
        data=data,
    )
    try:
        persisted = (await asyncio.to_thread(store.append, session_id, [item]))[0]
    except Exception:  # a missing marker must not fail the write it marks
        _logger.warning("Could not write a task marker for %s", session_id, exc_info=True)
        return
    if not persisted.deduplicated:
        event = OutputItemDoneEvent(type="response.output_item.done", item=persisted.to_api_dict())
        session_stream.publish(session_id, event.model_dump())
