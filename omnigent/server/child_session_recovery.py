"""Restore interrupted child sessions after their parent has initialized."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar
from weakref import WeakValueDictionary

import httpx

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.entities import Conversation
from omnigent.harness_plugins import native_agents
from omnigent.harnesses.codex_native.side_chat import is_side_chat_child
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.stores.conversation_store import ConversationNotFoundError, ConversationStore
from omnigent.util.session_lifecycle import is_session_closed

_logger = logging.getLogger(__name__)
RECOVERY_STORE_CONCURRENCY = 8
_P = ParamSpec("_P")
_T = TypeVar("_T")
_child_recovery_locks: WorkspaceScopedCache[str, asyncio.Lock] = WorkspaceScopedCache(
    WeakValueDictionary
)

_restoration_tasks: WorkspaceScopedCache[tuple[str, str, int], asyncio.Task[None]] = (
    WorkspaceScopedCache()
)


def schedule_child_restoration(
    parent: Conversation,
    client: httpx.AsyncClient,
    store: ConversationStore,
    initializer: RunnerSessionInitializer,
) -> None:
    """Restore children after parent readiness without delaying its next message."""
    if parent.runner_id is None:
        return
    generation = initializer.generation_for(parent.runner_id, client)
    key = (parent.id, parent.runner_id, generation)
    existing = _restoration_tasks.get(key)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        restore_active_children(parent, client, store, initializer, generation=generation),
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
        and (conv.runner_id is None or _intentional_stop_sessions.get(conv.id) != conv.runner_id)
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
    *,
    generation: int | None = None,
    store_slots: asyncio.Semaphore | None = None,
) -> None:
    """Rebind and initialize interrupted descendants on their recovered parent's runner."""
    from omnigent.runtime import get_runner_router
    from omnigent.server.routes.sessions import _ensure_runner_relay

    if parent.runner_id is None or not _restorable(parent):
        return
    runner_id = parent.runner_id
    if generation is None:
        generation = initializer.generation_for(runner_id, client)
    if initializer.generation_for(runner_id, client) != generation:
        _logger.info("Stopped restoring children of %s: runner tunnel changed", parent.id)
        return
    router = get_runner_router()
    runner_owner = router.runner_owner(parent.runner_id) if router is not None else None

    # A reconnect can share this database budget across its independent trees.
    if store_slots is None:
        store_slots = asyncio.Semaphore(RECOVERY_STORE_CONCURRENCY)

    async def store_call(fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        async with store_slots:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def ownership_allows(row: Conversation) -> bool:
        if runner_owner is None:
            return True
        session_owner = await store_call(store.get_session_owner, row.id, owner_only=True)
        # Internal children without an owner grant inherit from their restored ancestor.
        return session_owner is None or session_owner == runner_owner

    if not await ownership_allows(parent):
        return
    # Include idle ancestors only when needed to host an interrupted descendant.
    tree: dict[str, Conversation] = {parent.id: parent}
    frontier = [parent.id]
    while frontier:
        children = await store_call(store.list_child_conversation_ids_by_parent, frontier)
        rows = await store_call(
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
                or is_side_chat_child(row.labels)
            ):
                continue
            tree[row.id] = row
            frontier.append(row.id)
    if initializer.generation_for(runner_id, client) != generation:
        _logger.info("Stopped restoring children of %s: runner tunnel changed", parent.id)
        return
    active = {row.id for row in tree.values() if row.id != parent.id and _interrupted(row)}
    needed = set(active)
    for row in reversed(list(tree.values())):
        if row.id in needed and row.parent_conversation_id in tree:
            needed.add(row.parent_conversation_id)

    # Same-runner mirror subtrees only need relays, fresh bindings, and ownership checks.
    protocol_needed = {
        row.id
        for row in tree.values()
        if row.id in needed and (row.runner_id != runner_id or not is_parent_owned_subagent(row))
    }
    for row in reversed(list(tree.values())):
        if row.id in protocol_needed and row.parent_conversation_id in tree:
            protocol_needed.add(row.parent_conversation_id)

    restorations: dict[str, asyncio.Task[bool]] = {}
    mirror_batch: tuple[set[str], asyncio.Future[dict[str, Conversation]]] | None = None

    async def read_mirror_batch(
        ids: set[str], result: asyncio.Future[dict[str, Conversation]]
    ) -> None:
        nonlocal mirror_batch
        await asyncio.sleep(0)
        mirror_batch = None
        try:
            rows = await store_call(store.get_conversations, list(ids))
        except Exception as error:  # noqa: BLE001 - raised by the waiting restorations.
            if not result.done():
                result.set_exception(error)
        else:
            if not result.done():
                result.set_result(rows)

    async def fresh_mirror_rows(snapshot: Conversation) -> dict[str, Conversation]:
        nonlocal mirror_batch
        # Coalesce mirrors that are ready after their ancestor and ownership waits.
        if mirror_batch is None:
            mirror_batch = (set(), asyncio.get_running_loop().create_future())
            group.create_task(read_mirror_batch(*mirror_batch))
        ids, result = mirror_batch
        ids.add(snapshot.id)
        assert snapshot.parent_conversation_id is not None
        ids.add(snapshot.parent_conversation_id)
        return await result

    def log_restore_failure(session_id: str, level: int) -> None:
        if initializer.generation_for(runner_id, client) != generation:
            _logger.info("Stopped restoring child session %s: runner tunnel changed", session_id)
        else:
            _logger.log(level, "Failed to restore child session %s", session_id, exc_info=True)

    async def restore(snapshot: Conversation) -> bool:
        ancestor = restorations.get(snapshot.parent_conversation_id or "")
        if ancestor is not None and not await ancestor:
            return False
        initializer.require_generation(runner_id, client, generation)
        # Re-read and initialize under one lock so competing restores share readiness.
        async with _child_recovery_locks.setdefault(snapshot.id, asyncio.Lock()):
            assert snapshot.parent_conversation_id is not None
            mirror_only = snapshot.id not in protocol_needed
            if mirror_only:
                if not await ownership_allows(snapshot):
                    return False
                rows = await fresh_mirror_rows(snapshot)
                owner = rows.get(snapshot.parent_conversation_id)
                child = rows.get(snapshot.id)
            else:
                owner = await store_call(store.get_conversation, snapshot.parent_conversation_id)
                child = await store_call(store.get_conversation, snapshot.id)
            if (
                owner is None
                or owner.runner_id != parent.runner_id
                or not _restorable(owner)
                or child is None
                or child.runner_id is None
                or child.runner_id != snapshot.runner_id
                or child.parent_conversation_id != owner.id
                or child.host_id is not None
                or is_side_chat_child(child.labels)
                or not _restorable(child)
                or (snapshot.id in active and not _interrupted(child))
                or (mirror_only and not is_parent_owned_subagent(child))
            ):
                return False
            if not mirror_only and not await ownership_allows(child):
                return False
            initializer.require_generation(runner_id, client, generation)
            try:
                if child.runner_id != parent.runner_id:
                    if router is not None and router.runner_is_online(child.runner_id):
                        return False
                    previous_runner_id = child.runner_id
                    child = await store_call(
                        store.replace_runner_id,
                        child.id,
                        parent.runner_id,
                        expected_runner_id=previous_runner_id,
                    )
                    if child.runner_id != parent.runner_id:
                        return False
                    await asyncio.gather(
                        *initializer.invalidate_session(child.id, runner_id=previous_runner_id),
                        return_exceptions=True,
                    )
                mirrored = is_parent_owned_subagent(child)
                if not mirrored:
                    response = await initializer.initialize(
                        child,
                        client,
                        timeout=10.0,
                        suppress_recovery_turn=not _interrupted(child),
                        resume_interrupted_turn=_interrupted(child),
                        generation=generation,
                        store_slots=store_slots,
                    )
                    response.raise_for_status()
                initializer.require_generation(runner_id, client, generation)
                _ensure_runner_relay(child.id, parent.runner_id, client, store, conversation=child)
                # Only execution status can clear the interruption. Initialization
                # may return before a native continuation emits its first running edge.
                return True
            except (httpx.HTTPError, ConnectionError, ConversationNotFoundError):
                log_restore_failure(snapshot.id, logging.WARNING)
                return False

    async def restore_safely(snapshot: Conversation) -> bool:
        try:
            return await restore(snapshot)
        except ConnectionError:
            log_restore_failure(snapshot.id, logging.ERROR)
            return False
        except Exception:
            # A failed branch must not cancel sibling restoration in the TaskGroup.
            _logger.exception("Failed to restore child session %s", snapshot.id)
            return False

    # Each child waits only for its ancestor. Slow siblings cannot strand a tree,
    # and TaskGroup joins all descendants on cancellation of the owning recovery.
    async with asyncio.TaskGroup() as group:
        for snapshot in tree.values():
            if snapshot.id != parent.id and snapshot.id in needed:
                restorations[snapshot.id] = group.create_task(
                    restore_safely(snapshot), name=f"restore-child-{snapshot.id}"
                )
