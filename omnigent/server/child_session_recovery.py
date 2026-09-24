"""Restore interrupted child sessions after their parent has initialized."""

from __future__ import annotations

import asyncio
import logging
from weakref import WeakValueDictionary

import httpx

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.entities import Conversation
from omnigent.harness_plugins import native_agents
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.stores.conversation_store import ConversationNotFoundError, ConversationStore
from omnigent.util.session_lifecycle import is_session_closed

_logger = logging.getLogger(__name__)
_child_recovery_locks: WorkspaceScopedCache[str, asyncio.Lock] = WorkspaceScopedCache(
    WeakValueDictionary
)

_restoration_tasks: WorkspaceScopedCache[tuple[str, str | None], asyncio.Task[None]] = (
    WorkspaceScopedCache()
)


def schedule_child_restoration(
    parent: Conversation,
    client: httpx.AsyncClient,
    store: ConversationStore,
    initializer: RunnerSessionInitializer,
) -> None:
    """Restore children after parent readiness without delaying its next message."""
    key = (parent.id, parent.runner_id)
    existing = _restoration_tasks.get(key)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        restore_active_children(parent, client, store, initializer),
        name=f"restore-children-{parent.id}",
    )
    _restoration_tasks[key] = task

    def finished(done: asyncio.Task[None]) -> None:
        if _restoration_tasks.get(key) is done:
            _restoration_tasks.pop(key, None)
        if not done.cancelled() and (error := done.exception()) is not None:
            _logger.error("Failed to restore children of %s", parent.id, exc_info=error)

    task.add_done_callback(finished)


def is_parent_owned_subagent(conv: Conversation) -> bool:
    """Native mirrors belong to their parent's runtime, not a separate terminal."""
    from omnigent.server.routes._sessions.common import (
        _ACP_SUBAGENT_ID_LABEL_KEY,
        _ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    )

    wrapper = conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    return conv.kind == "sub_agent" and (
        bool(conv.labels.get(_ACP_SUBAGENT_ID_LABEL_KEY))
        or wrapper == _ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE
        or (
            wrapper is not None
            and any(wrapper == agent.subagent_wrapper_label for agent in native_agents())
        )
    )


def _restorable(conv: Conversation) -> bool:
    from omnigent.server.routes._sessions.common import (
        _intentional_stop_sessions,
        _interrupt_fenced_sessions,
    )

    return (
        conv.agent_id is not None
        and not conv.archived
        and not is_session_closed(conv.labels, conv.title)
        and conv.id not in _intentional_stop_sessions
        and conv.id not in _interrupt_fenced_sessions
    )


def _interrupted(conv: Conversation) -> bool:
    from omnigent.server.routes._sessions.common import _session_status_cache
    from omnigent.server.routes._sessions.helpers import _last_task_error_from_labels

    status = _session_status_cache.get(conv.id, conv.live_status)
    error = _last_task_error_from_labels(conv.labels)
    return status in {"running", "waiting"} or (
        status == "failed"
        and error is not None
        and error.get("code") in {"runner_disconnected", "runner_failed_to_start"}
    )


async def restore_active_children(
    parent: Conversation,
    client: httpx.AsyncClient,
    store: ConversationStore,
    initializer: RunnerSessionInitializer,
) -> None:
    """Rebind and initialize interrupted descendants on their recovered parent's runner."""
    from omnigent.runtime import get_runner_router
    from omnigent.server.routes._sessions.common import _RUNNER_SESSION_INIT_TIMEOUT
    from omnigent.server.routes.sessions import _ensure_runner_relay

    if parent.runner_id is None or not _restorable(parent):
        return
    router = get_runner_router()
    runner_owner = router.runner_owner(parent.runner_id) if router is not None else None

    async def ownership_allows(row: Conversation) -> bool:
        if runner_owner is None:
            return True
        session_owner = await asyncio.to_thread(store.get_session_owner, row.id, owner_only=True)
        # Internal children without an owner grant inherit from their restored ancestor.
        return session_owner is None or session_owner == runner_owner

    if not await ownership_allows(parent):
        return
    # Include idle ancestors only when needed to host an interrupted descendant.
    tree: dict[str, Conversation] = {parent.id: parent}
    frontier = [parent.id]
    while frontier:
        children = await asyncio.to_thread(store.list_child_conversation_ids_by_parent, frontier)
        rows = await asyncio.to_thread(
            store.get_conversations, [child for ids in children.values() for child in ids]
        )
        frontier = []
        for row in rows.values():
            if (
                row.id in tree
                or row.host_id is not None
                or row.runner_id is None
                or row.parent_conversation_id not in tree
                or (
                    row.runner_id != parent.runner_id
                    and router is not None
                    and router.runner_is_online(row.runner_id)
                )
                or not _restorable(row)
            ):
                continue
            tree[row.id] = row
            frontier.append(row.id)
    active = {row.id for row in tree.values() if row.id != parent.id and _interrupted(row)}
    needed = set(active)
    for row in reversed(list(tree.values())):
        if row.id in needed and row.parent_conversation_id in tree:
            needed.add(row.parent_conversation_id)

    restored = {parent.id}
    for snapshot in tree.values():
        if snapshot.id == parent.id or snapshot.id not in needed:
            continue
        # Re-read and initialize under one lock so competing restores share readiness.
        async with _child_recovery_locks.setdefault(snapshot.id, asyncio.Lock()):
            assert snapshot.parent_conversation_id is not None
            owner = await asyncio.to_thread(
                store.get_conversation, snapshot.parent_conversation_id
            )
            child = await asyncio.to_thread(store.get_conversation, snapshot.id)
            if (
                owner is None
                or owner.id not in restored
                or owner.runner_id != parent.runner_id
                or not _restorable(owner)
                or child is None
                or child.runner_id is None
                or child.runner_id != snapshot.runner_id
                or child.parent_conversation_id != owner.id
                or child.host_id is not None
                or not _restorable(child)
                or (snapshot.id in active and not _interrupted(child))
            ):
                continue
            if not await ownership_allows(child):
                continue
            try:
                if child.runner_id != parent.runner_id:
                    if router is not None and router.runner_is_online(child.runner_id):
                        continue
                    child = await asyncio.to_thread(
                        store.replace_runner_id,
                        child.id,
                        parent.runner_id,
                        expected_runner_id=child.runner_id,
                    )
                    if child.runner_id != parent.runner_id:
                        continue
                    initializer.invalidate_session(child.id)
                mirrored = is_parent_owned_subagent(child)
                if not mirrored:
                    response = await initializer.initialize(
                        child,
                        client,
                        timeout=_RUNNER_SESSION_INIT_TIMEOUT,
                        suppress_recovery_turn=not _interrupted(child),
                        resume_interrupted_turn=_interrupted(child),
                    )
                    response.raise_for_status()
                _ensure_runner_relay(child.id, parent.runner_id, client, store)
                # Only execution status can clear the interruption. Initialization
                # may return before a native continuation emits its first running edge.
                restored.add(child.id)
            except (httpx.HTTPError, ConnectionError, ConversationNotFoundError):
                _logger.warning("Failed to restore child session %s", snapshot.id, exc_info=True)
