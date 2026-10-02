"""Durable parent-chat links for subagent delegation and returned results."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Literal

from omnigent.entities import (
    Conversation,
    ConversationItem,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
    ResourceEventData,
)
from omnigent.runtime import session_stream
from omnigent.server.schemas import OutputItemDoneEvent
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)
_TASK_NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
_TERMINAL_TASK_STATUS_RE = re.compile(
    r"<status>\s*(completed|failed|cancelled|killed)\s*</status>"
)
_TASK_ID_RE = re.compile(r"<(task-id|tool-use-id)>\s*([^<]+?)\s*</\1>")
_COMPLETION_EVENT_TYPE = "session.subagent.completion-observed"
_COMPLETION_RESOURCE_TYPE = "subagent_completion"
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def native_subagent_terminal_status(
    status: str,
    *,
    harness: str | None,
    turn_outcome: object = None,
    turn_completed: object = None,
) -> str | None:
    """Respect confirmed outcomes; a bare Claude idle is only an observation."""
    if status not in {"idle", "failed"}:
        return None
    if isinstance(turn_outcome, str) and turn_outcome in {"completed", "failed", "cancelled"}:
        return turn_outcome
    if status == "failed":
        return "failed"
    if harness in {None, "claude-native"} and turn_completed is not True:
        return None
    return "completed"


def _title(child: Conversation) -> str:
    named = (
        child.task_summary
        or child.labels.get("omnigent.claude_native.subagent_name")
        or child.labels.get("omnigent.claude_native.description")
        or child.labels.get("omnigent.codex_native.agent_nickname")
        or child.labels.get("omnigent.codex_native.agent_role")
    )
    if named:
        return named
    title = child.title or ""
    if child.labels.get("omnigent.wrapper", "").endswith("-subagent"):
        if child.sub_agent_name:
            return child.sub_agent_name.rpartition(":")[2]
        prefix = title.partition(":")[0]
        if prefix.endswith("-native-ui-subagent"):
            return "Sub-agent"
        return prefix or child.sub_agent_name or "Sub-agent"
    if title.startswith("ui:"):
        return title.split(":", 2)[-1]
    return title.partition(":")[2] or title or child.sub_agent_name or "Sub-agent"


def _completion_marker_id(kind: str, native_id: str) -> str:
    key = f"claude-subagent-completion:{kind}:{native_id}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _completion_marker(kind: str, native_id: str, status: str) -> NewConversationItem:
    stable_id = _completion_marker_id(kind, native_id)
    return NewConversationItem(
        type="resource_event",
        stable_id=stable_id,
        response_id="subagent_" + stable_id,
        data=ResourceEventData(
            event_type=_COMPLETION_EVENT_TYPE,
            resource_id=native_id,
            resource_type=_COMPLETION_RESOURCE_TYPE,
            resource={"kind": kind, "status": status},
        ),
    )


def _completion_status(
    parent_id: str,
    kind: str,
    native_id: str,
    store: ConversationStore,
) -> str | None:
    marker = store.get_item(parent_id, _completion_marker_id(kind, native_id))
    if marker is None or not isinstance(marker.data, ResourceEventData):
        return None
    resource = marker.data.resource
    status = resource.get("status") if isinstance(resource, dict) else None
    if (
        marker.data.event_type != _COMPLETION_EVENT_TYPE
        or marker.data.resource_id != native_id
        or marker.data.resource_type != _COMPLETION_RESOURCE_TYPE
        or not isinstance(resource, dict)
        or resource.get("kind") != kind
        or not isinstance(status, str)
        or status not in _TERMINAL_STATUSES
    ):
        return None
    return status


async def _recorded_completion_status(
    parent_id: str,
    child: Conversation,
    store: ConversationStore,
) -> str | None:
    for kind, label in (
        ("task", "omnigent.claude_native.subagent_id"),
        ("call", "omnigent.claude_native.tool_use_id"),
    ):
        native_id = child.labels.get(label)
        if native_id:
            status = await asyncio.to_thread(_completion_status, parent_id, kind, native_id, store)
            if status:
                return status
    return None


async def record_subagent_activity(
    child_id: str,
    phase: Literal["delegated", "returned"],
    store: ConversationStore,
    *,
    parent_id: str | None = None,
    turn_id: str | None = None,
    status: str | None = None,
) -> None:
    """Persist and publish a child lifecycle edge once, including across retries."""
    try:
        child = await asyncio.to_thread(store.get_conversation, child_id)
        if child is None or child.parent_conversation_id is None:
            return
        if parent_id is not None and child.parent_conversation_id != parent_id:
            return
        parent_id = child.parent_conversation_id
        if (
            phase == "delegated"
            and child.labels.get("omnigent.wrapper") == "codex-native-ui-subagent"
            and not child.labels.get("omnigent.codex_native.agent_nickname")
            and not child.labels.get("omnigent.codex_native.agent_role")
        ):
            # Codex registers the child before thread/resume supplies its name.
            return
        if phase == "returned" and turn_id is None:
            latest = await asyncio.to_thread(store.list_items, child.id, limit=20, order="desc")
            turn_id = next(
                (
                    row.response_id
                    for row in latest.data
                    if (
                        isinstance(row.data, MessageData)
                        and (row.data.role == "assistant" or status == "failed")
                        and not row.data.is_meta
                    )
                    or row.type in {"function_call", "function_call_output"}
                ),
                None,
            )
            if turn_id is None:
                if status != "failed":
                    return
                # A runner can die before producing any transcript for its first turn.
                turn_id = child.id
        key = f"{child.id}:{phase}:{turn_id or ''}"
        stable_id = hashlib.sha256(key.encode()).hexdigest()[:32]
        item = NewConversationItem(
            type="resource_event",
            stable_id=stable_id,
            response_id="subagent_" + stable_id,
            data=ResourceEventData(
                event_type=f"session.subagent.{phase}",
                resource_id=child.id,
                resource_type="session",
                resource={"title": _title(child), **({"status": status} if status else {})},
            ),
        )
        persisted = (await asyncio.to_thread(store.append, parent_id, [item]))[0]
        if not persisted.deduplicated:
            event = OutputItemDoneEvent(
                type="response.output_item.done", item=persisted.to_api_dict()
            )
            session_stream.publish(parent_id, event.model_dump())
        if phase == "delegated" and any(
            child.labels.get(label)
            for label in (
                "omnigent.claude_native.subagent_id",
                "omnigent.claude_native.tool_use_id",
            )
        ):
            # A quick result may reach the parent before child discovery runs.
            completion_status = await _recorded_completion_status(parent_id, child, store)
            if completion_status:
                await record_subagent_activity(
                    child.id,
                    "returned",
                    store,
                    parent_id=parent_id,
                    turn_id=child.labels.get("omnigent.claude_native.tool_use_id") or child.id,
                    status=completion_status,
                )
    except Exception:  # noqa: BLE001 — display metadata must not interrupt child delivery
        _logger.warning("Could not record subagent activity for %s", child_id, exc_info=True)


async def record_claude_subagent_return(
    parent_id: str,
    item: NewConversationItem | ConversationItem,
    store: ConversationStore,
) -> None:
    """Match an actual Claude result to its child; launch handles are not results."""
    try:
        await _record_claude_subagent_return(parent_id, item, store)
    except Exception:  # noqa: BLE001 — optional correlation must not interrupt transcript delivery
        _logger.warning("Could not match Claude subagent result in %s", parent_id, exc_info=True)


def _claude_completion_ids(
    item: NewConversationItem | ConversationItem,
) -> tuple[dict[str, str], dict[str, str]]:
    task_ids: dict[str, str] = {}
    call_ids: dict[str, str] = {}
    if isinstance(item.data, FunctionCallOutputData) or (
        isinstance(item.data, MessageData) and item.data.is_meta
    ):
        if item.data.subagent_return_id:
            task_ids[item.data.subagent_return_id] = "completed"
    if isinstance(item.data, MessageData) and item.data.is_meta:
        for block in item.data.content:
            text = block.get("text")
            if not isinstance(text, str):
                continue
            for notification in _TASK_NOTIFICATION_RE.finditer(text):
                body = notification.group(1)
                status_match = _TERMINAL_TASK_STATUS_RE.search(body)
                if status_match is None:
                    continue
                status = status_match.group(1)
                if status == "killed":
                    status = "cancelled"
                for key, value in _TASK_ID_RE.findall(body):
                    (task_ids if key == "task-id" else call_ids)[value] = status
    return task_ids, call_ids


def claude_subagent_completion_markers(
    item: NewConversationItem | ConversationItem,
) -> list[NewConversationItem]:
    """Build stable markers to commit in the same transaction as the result."""
    task_ids, call_ids = _claude_completion_ids(item)
    return [
        _completion_marker(kind, native_id, status)
        for kind, completions in (("task", task_ids), ("call", call_ids))
        for native_id, status in completions.items()
    ]


async def _record_claude_subagent_return(
    parent_id: str,
    item: NewConversationItem | ConversationItem,
    store: ConversationStore,
) -> None:
    task_ids, call_ids = _claude_completion_ids(item)
    if not task_ids and not call_ids:
        return
    after: str | None = None
    while True:
        page = await asyncio.to_thread(
            store.list_conversations,
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            status = task_ids.get(
                child.labels.get("omnigent.claude_native.subagent_id", "")
            ) or call_ids.get(child.labels.get("omnigent.claude_native.tool_use_id", ""))
            if status:
                await record_subagent_activity(
                    child.id,
                    "returned",
                    store,
                    parent_id=parent_id,
                    turn_id=child.labels.get("omnigent.claude_native.tool_use_id") or child.id,
                    status=status,
                )
        if not page.has_more or page.last_id is None:
            return
        after = page.last_id
