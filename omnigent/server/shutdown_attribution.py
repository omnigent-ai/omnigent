"""Shutdown evidence scoped to a runner connection and session lifecycle.

Private metadata shares a row with live status: another replica can observe a
stop, and settlement cannot overwrite a newer lifecycle or a preceding failure.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import debug_event
from omnigent.entities import Conversation
from omnigent.host.shutdown import (
    SHUTDOWN_CLOCK_SKEW_MS,
    SHUTDOWN_INTENT_TTL_MS,
    ShutdownIntent,
    timestamp_ms,
)
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostConnection, HostRegistry


class SessionScope(BaseModel):
    """The exact connection and lifecycle which may be ended by a stop."""

    model_config = ConfigDict(frozen=True)
    lifecycle_id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=64)
    runner_id: str = Field(max_length=128)
    runner_connection_id: str = Field(min_length=1, max_length=128)
    response_id: str | None = Field(default=None, max_length=256)


class SessionShutdown(BaseModel):
    """A single stop's evidence, never a session-wide error exclusion."""

    model_config = ConfigDict(frozen=True)
    intent: ShutdownIntent
    scope: SessionScope
    recorded_at_ms: int
    preserve_failure: bool = False

    def log_attrs(self) -> dict[str, Any]:
        """Identity and ordering fields for an individual attributable error row."""
        return {
            **self.intent.log_attrs(),
            **self.scope.model_dump(),
            "shutdown_recorded_at_ms": self.recorded_at_ms,
        }


class ShutdownRecords(list[tuple[str, SessionShutdown]]):
    """One attempt's scoped evidence and the state to restore after rejection."""

    def __init__(self) -> None:
        super().__init__()
        self.previous: dict[str, SessionShutdown] = {}
        self.completed_relays: dict[str, tuple[Any, int]] = {}


_RUNNER_STOP_STATUS_BATCH_SIZE = 200


session_scopes: WorkspaceScopedCache[str, SessionScope] = WorkspaceScopedCache()
session_shutdowns: WorkspaceScopedCache[str, SessionShutdown] = WorkspaceScopedCache()
runner_connections: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


def forget_connection(runner_id: str, connection_id: str) -> None:
    """Drop only the departed incarnation's local hints after a replica handoff."""
    from omnigent.server.session_live_state import forget_live_status

    if runner_connections.get(runner_id) == connection_id:
        runner_connections.pop(runner_id, None)
    for session_id, scope in list(session_scopes.items()):
        if scope.runner_id == runner_id and scope.runner_connection_id == connection_id:
            session_scopes.pop(session_id, None)
            session_shutdowns.pop(session_id, None)
            forget_live_status(session_id)


async def scope_is_current(
    session_id: str,
    store: ConversationStore,
    *,
    scope: SessionScope | None = None,
    connection_id: str | None = None,
    runner_id: str | None = None,
) -> bool:
    """Do not let an old disconnect fail a replacement connection or newer turn."""
    try:
        state = await asyncio.to_thread(store.get_shutdown_state, session_id)
        raw = state.get("scope") if state is not None else None
        current = SessionScope.model_validate_json(raw) if raw else None
    except Exception:  # noqa: BLE001 — an unavailable read cannot prove recovery
        return True
    if state is not None and runner_id is not None and state["runner_id"] != runner_id:
        return False
    if current is None:
        return scope is None
    return (scope is None or current == scope) and (
        connection_id is None or current.runner_connection_id == connection_id
    )


async def record_host_shutdown(
    conn: HostConnection,
    intent: ShutdownIntent,
    runner_ids: list[str],
    registry: HostRegistry,
    store: ConversationStore,
    *,
    request_user_id: str | None = None,
) -> bool:
    """Accept evidence only from the current, authenticated host incarnation."""
    from omnigent.server.host_registry import _canonical_host_id

    def is_current() -> bool:
        return registry.get(conn.host_id) is conn

    if (
        not is_current()
        or not conn.hello.process_id
        or not conn.hello.connection_id
        or intent.host_id is None
        or _canonical_host_id(intent.host_id) != conn.host_id
        or intent.host_process_id != conn.hello.process_id
        or intent.host_connection_id != conn.hello.connection_id
        or not -SHUTDOWN_CLOCK_SKEW_MS
        <= timestamp_ms() - intent.requested_at_ms
        <= SHUTDOWN_INTENT_TTL_MS + SHUTDOWN_CLOCK_SKEW_MS
        or intent.reason == "user_stopped_session"
    ):
        return False
    intent = intent.model_copy(
        update={
            "host_id": conn.host_id,
            "initiator_user_id": request_user_id if intent.action != "signal" else None,
        }
    )
    affected = sorted(set(runner_ids) & conn.runner_ids)
    _logger.info(
        "%s",
        intent.category,
        extra=debug_event("host_shutdown_requested", **intent.log_attrs(), runner_ids=affected),
    )
    for runner_id in affected:
        if not is_current():
            return False
        await record_runner_shutdown(runner_id, intent, store, is_current=is_current)
    return is_current()


async def _start_scope(
    session_id: str,
    store: ConversationStore,
    scope: SessionScope | None,
    response_id: str | None = None,
    expected_connection_id: str | None = None,
    is_current: Callable[[], bool] | None = None,
    expected_scope: SessionScope | None = None,
) -> SessionScope | None:
    from omnigent.server.session_live_state import forget_live_status

    if not hasattr(store, "get_shutdown_state"):
        return None
    for _ in range(3):
        state = await asyncio.to_thread(store.get_shutdown_state, session_id)
        if state is None or (is_current is not None and not is_current()):
            return None
        previous = state.get("scope")
        previous_scope = SessionScope.model_validate_json(previous) if previous else None
        if expected_scope is not None and previous_scope != expected_scope:
            return None
        if expected_connection_id is not None and (
            previous_scope is None or previous_scope.runner_connection_id != expected_connection_id
        ):
            return None
        if (
            scope is None
            and response_id is not None
            and previous_scope is not None
            and previous_scope.response_id == response_id
        ):
            session_scopes[session_id] = previous_scope
            return previous_scope
        candidate = scope
        if scope is None and previous:
            candidate = SessionScope.model_validate_json(previous).model_copy(
                update={
                    "lifecycle_id": uuid.uuid4().hex,
                    "response_id": response_id,
                }
            )
        if candidate is not None and candidate.runner_id != state["runner_id"]:
            return None
        if (
            scope is not None
            and previous_scope is not None
            and (
                scope.runner_id == previous_scope.runner_id
                and scope.runner_connection_id == previous_scope.runner_connection_id
            )
        ):
            session_scopes[session_id] = previous_scope
            return previous_scope
        applied = await asyncio.to_thread(
            store.compare_shutdown_state,
            session_id,
            runner_id=state["runner_id"],
            expected_scope=previous,
            scope=candidate.model_dump_json() if candidate else None,
            intent=None,
        )
        if applied:
            if is_current is not None and not is_current():
                return None
            session_shutdowns.pop(session_id, None)
            forget_live_status(session_id)
            if candidate is not None:
                session_scopes[session_id] = candidate
                _logger.info(
                    "Runner session lifecycle started",
                    extra=debug_event(
                        "session_lifecycle_started",
                        session_id=session_id,
                        **candidate.model_dump(),
                    ),
                )
            else:
                session_scopes.pop(session_id, None)
            return candidate
        if scope is not None and is_current is None:
            return None
    return None


async def begin_connection(
    session_id: str,
    runner_id: str,
    connection_id: str,
    store: ConversationStore,
    *,
    is_current: Callable[[], bool] | None = None,
) -> SessionScope | None:
    """Start a fresh scope before consuming events on a runner stream."""
    return await _start_scope(
        session_id,
        store,
        SessionScope(runner_id=runner_id, runner_connection_id=connection_id),
        is_current=is_current,
    )


async def advance_lifecycle(
    session_id: str,
    store: ConversationStore,
    response_id: str | None = None,
    *,
    connection_id: str | None = None,
    expected_scope: SessionScope | None = None,
) -> SessionScope | None:
    """Disarm persisted intent before a new turn, including on a cold replica."""
    return await _start_scope(
        session_id, store, None, response_id, connection_id, expected_scope=expected_scope
    )


async def refresh_scope(
    session_id: str,
    store: ConversationStore,
    *,
    connection_id: str | None,
) -> SessionScope | None:
    """Observe another replica's new turn without treating PTY activity as one."""
    from omnigent.server.session_live_state import forget_live_status

    if connection_id is None or not hasattr(store, "get_shutdown_state"):
        return None
    try:
        state = await asyncio.to_thread(store.get_shutdown_state, session_id)
        raw = state.get("scope") if state is not None else None
        current = SessionScope.model_validate_json(raw) if raw else None
        if (
            current is None
            or current.runner_connection_id != connection_id
            or state is None
            or current.runner_id != state["runner_id"]
        ):
            return None
        if session_scopes.get(session_id) != current:
            session_scopes[session_id] = current
            session_shutdowns.pop(session_id, None)
            forget_live_status(session_id)
        return current
    except Exception:  # noqa: BLE001 — observation must not interrupt a live stream
        return None


def has_local_intent(session_id: str, runner_id: str | None = None) -> bool:
    """Peek at a current local marker without consuming another relay's evidence."""
    evidence = session_shutdowns.get(session_id)
    scope = session_scopes.get(session_id)
    return (
        evidence is not None
        and evidence.scope == scope
        and (runner_id is None or evidence.scope.runner_id == runner_id)
        and 0 <= timestamp_ms() - evidence.recorded_at_ms <= SHUTDOWN_INTENT_TTL_MS
    )


async def record_session_shutdown(
    conv: Conversation,
    intent: ShutdownIntent,
    store: ConversationStore,
    *,
    arm: bool = True,
    is_current: Callable[[], bool] | None = None,
) -> SessionShutdown | None:
    """Persist intent before teardown, even when it will produce no failure."""
    from omnigent.server.routes._sessions.common import _session_status_cache
    from omnigent.server.routes._sessions.helpers import _last_task_error_from_labels

    scope = None
    evidence = None
    try:
        state = (
            await asyncio.to_thread(store.get_shutdown_state, conv.id)
            if hasattr(store, "get_shutdown_state")
            else None
        )
        raw_scope = state.get("scope") if state is not None else None
        scope = SessionScope.model_validate_json(raw_scope) if raw_scope else None
        eligible = (
            arm
            and (is_current is None or is_current())
            and intent.requested
            and scope is not None
            and scope.runner_id == conv.runner_id
            and -SHUTDOWN_CLOCK_SKEW_MS
            <= timestamp_ms() - intent.requested_at_ms
            <= SHUTDOWN_INTENT_TTL_MS + SHUTDOWN_CLOCK_SKEW_MS
            and state is not None
            and state["runner_id"] == conv.runner_id
            and (session_scopes.get(conv.id) is None or session_scopes.get(conv.id) == scope)
            and (
                runner_connections.get(scope.runner_id) is None
                or runner_connections.get(scope.runner_id) == scope.runner_connection_id
            )
        )
        evidence = None
        if eligible and scope is not None and state is not None:
            evidence = SessionShutdown(
                intent=intent,
                scope=scope,
                recorded_at_ms=timestamp_ms(),
                preserve_failure=(
                    _session_status_cache.get(conv.id, state["live_status"]) == "failed"
                    or _last_task_error_from_labels(conv.labels) is not None
                ),
            )
            if state.get("intent"):
                previous = SessionShutdown.model_validate_json(state["intent"])
                if previous.intent.shutdown_id == intent.shutdown_id and previous.scope == scope:
                    evidence = previous
            applied = await asyncio.to_thread(
                store.compare_shutdown_state,
                conv.id,
                runner_id=conv.runner_id,
                expected_scope=raw_scope,
                scope=raw_scope,
                intent=evidence.model_dump_json(),
                intent_id=intent.shutdown_id,
                intent_recorded_at_ms=evidence.recorded_at_ms,
            )
            if not applied or (is_current is not None and not is_current()):
                evidence = None
            elif session_scopes.get(conv.id, scope) == scope:
                session_shutdowns[conv.id] = evidence
    except Exception:  # noqa: BLE001 — evidence must never prevent an authorized Stop
        evidence = None
        _logger.warning(
            "Could not record shutdown evidence for %s",
            conv.id,
            extra=debug_event(
                "shutdown_attribution_failed", session_id=conv.id, operation="record"
            ),
            exc_info=True,
        )
    _logger.info(
        "Shutdown requested for session %s: %s",
        conv.id,
        intent.category,
        extra=debug_event(
            "session_shutdown_requested",
            session_id=conv.id,
            **intent.log_attrs(),
            **(scope.model_dump() if scope is not None else {"runner_id": conv.runner_id}),
            eligible=evidence is not None,
            shutdown_recorded_at_ms=evidence.recorded_at_ms if evidence is not None else None,
        ),
    )
    return evidence


async def record_runner_shutdown(
    runner_id: str,
    intent: ShutdownIntent,
    store: ConversationStore,
    *,
    primary_session_id: str | None = None,
    is_current: Callable[[], bool] | None = None,
) -> ShutdownRecords:
    """Cover only sessions owned by the authenticated host and its stopped runner."""
    from omnigent.server.host_registry import _canonical_host_id
    from omnigent.server.routes._sessions.common import (
        _runner_relay_tasks,
        _session_status_cache,
    )

    statuses: dict[str, str | None] = {}
    after = None
    try:
        while True:
            batch = await asyncio.to_thread(
                store.list_runner_session_statuses,
                runner_id,
                after=after,
                limit=_RUNNER_STOP_STATUS_BATCH_SIZE,
            )
            if is_current is not None and not is_current():
                return ShutdownRecords()
            statuses.update(batch)
            if len(batch) < _RUNNER_STOP_STATUS_BATCH_SIZE:
                break
            after = batch[-1][0]
    except Exception:  # noqa: BLE001 — evidence must not prevent runner teardown
        _logger.warning(
            "Could not find all shutdown sessions for runner %s; using partial results and relays",
            runner_id,
            exc_info=True,
        )
    for session_id, handle in _runner_relay_tasks.items():
        if handle.runner_id == runner_id and not handle.task.done():
            statuses.setdefault(session_id, None)
    if primary_session_id is not None:
        statuses.setdefault(primary_session_id, None)
    result = ShutdownRecords()
    for session_id, persisted_status in statuses.items():
        if is_current is not None and not is_current():
            break
        handle = _runner_relay_tasks.get(session_id)
        if handle is not None and handle.runner_id != runner_id:
            continue
        if session_id != primary_session_id and _session_status_cache.get(
            session_id, persisted_status
        ) not in {"running", "waiting", None}:
            continue
        try:
            conv = await asyncio.to_thread(store.get_conversation, session_id)
        except Exception:  # noqa: BLE001 — unknown ownership cannot authorize attribution
            _logger.warning("Could not verify shutdown session %s", session_id, exc_info=True)
            continue
        if (
            conv is None
            or conv.runner_id != runner_id
            or (is_current is not None and not is_current())
        ):
            continue
        host_id = conv.host_id
        if host_id is None and conv.root_conversation_id and conv.root_conversation_id != conv.id:
            try:
                root = await asyncio.to_thread(store.get_conversation, conv.root_conversation_id)
            except Exception:  # noqa: BLE001 — unknown ownership cannot authorize attribution
                _logger.warning("Could not verify shutdown host for %s", conv.id, exc_info=True)
                continue
            host_id = root.host_id if root is not None else None
        if intent.host_id is not None and (
            host_id is None or _canonical_host_id(host_id) != _canonical_host_id(intent.host_id)
        ):
            continue
        if is_current is not None and not is_current():
            break
        await _initialize_child_scope(conv, store, is_current=is_current)
        previous = await matching_shutdown(conv.id, store, runner_id=runner_id)
        evidence = await record_session_shutdown(conv, intent, store, is_current=is_current)
        if evidence is not None:
            result.append((conv.id, evidence))
            if previous is not None:
                result.previous[conv.id] = previous
            if handle is not None:
                if handle.intentional_stop_turn_ended:
                    result.completed_relays[conv.id] = (handle, handle.running_event_count)
                handle.intentional_stop_turn_ended = False
    return result


async def _initialize_child_scope(
    conv: Conversation,
    store: ConversationStore,
    *,
    is_current: Callable[[], bool] | None = None,
) -> None:
    """Give a cold child the known current runner connection before recording Stop."""
    try:
        state = await asyncio.to_thread(store.get_shutdown_state, conv.id)
        if state is None or state.get("scope") or conv.runner_id is None:
            return
        connection_id = runner_connections.get(conv.runner_id)
        if connection_id is None and conv.root_conversation_id != conv.id:
            root_id = conv.root_conversation_id or conv.parent_conversation_id
            root_state = (
                await asyncio.to_thread(store.get_shutdown_state, root_id) if root_id else None
            )
            if root_state is not None and root_state["runner_id"] == conv.runner_id:
                raw = root_state.get("scope")
                if raw:
                    connection_id = SessionScope.model_validate_json(raw).runner_connection_id
        if connection_id is not None:
            await begin_connection(
                conv.id, conv.runner_id, connection_id, store, is_current=is_current
            )
    except Exception:  # noqa: BLE001 — missing connection identity leaves attribution ineligible
        _logger.warning("Could not initialize shutdown scope for %s", conv.id, exc_info=True)


async def prepare_session_stop(
    conv: Conversation,
    store: ConversationStore,
    registry: HostRegistry | None,
    *,
    user_id: str | None = None,
    action: Literal["stop_session", "archive"] = "stop_session",
    reported_intent: object = None,
) -> tuple[ShutdownIntent, ShutdownRecords]:
    """Record Stop before forwarding it to either the pane or its dedicated runner."""
    intent = ShutdownIntent(
        reason="user_stopped_session",
        action=action,
        initiator="authenticated_user",
        initiator_user_id=user_id,
        host_id=conv.host_id,
    )
    conn = registry.get(conv.host_id) if registry is not None and conv.host_id else None
    if reported_intent is not None and conn is not None:
        from omnigent.server.host_registry import _canonical_host_id

        try:
            reported = ShutdownIntent.model_validate(reported_intent)
        except ValueError:
            reported = None
        if (
            reported is not None
            and reported.requested
            and reported.reason == "user_stopped_host"
            and reported.host_id is not None
            and _canonical_host_id(reported.host_id) == conn.host_id
            and reported.host_process_id == conn.hello.process_id
            and reported.host_connection_id == conn.hello.connection_id
        ):
            intent = reported.model_copy(update={"initiator_user_id": user_id})
    if conn is not None:
        intent = intent.model_copy(
            update={
                "host_id": conn.host_id,
                "host_process_id": conn.hello.process_id,
                "host_connection_id": conn.hello.connection_id,
            }
        )
    if conv.host_id and conv.runner_id:
        evidence = await record_runner_shutdown(
            conv.runner_id, intent, store, primary_session_id=conv.id
        )
    else:
        await record_session_shutdown(conv, intent, store, arm=False)
        evidence = ShutdownRecords()
    return intent, evidence


async def cancel_shutdown(
    evidence: list[tuple[str, SessionShutdown]],
    store: ConversationStore,
) -> None:
    """Disarm an undelivered stop without clearing a concurrent replacement."""
    from omnigent.server.routes._sessions.common import _runner_relay_tasks

    for session_id, stop in evidence:
        if session_shutdowns.get(session_id) == stop:
            session_shutdowns.pop(session_id, None)
        scope = stop.scope.model_dump_json()
        previous = (
            evidence.previous.get(session_id) if isinstance(evidence, ShutdownRecords) else None
        )
        completed = (
            evidence.completed_relays.get(session_id)
            if isinstance(evidence, ShutdownRecords)
            else None
        )
        new_activity = completed is not None and completed[0].running_event_count != completed[1]
        if new_activity or (previous is not None and previous.scope != stop.scope):
            previous = None
        try:
            applied = await asyncio.to_thread(
                store.compare_shutdown_state,
                session_id,
                runner_id=stop.scope.runner_id,
                expected_scope=scope,
                scope=scope,
                intent=previous.model_dump_json() if previous else None,
                expected_intent=stop.model_dump_json(),
            )
            if applied:
                if previous is not None:
                    session_shutdowns[session_id] = previous
                if completed is not None and _runner_relay_tasks.get(session_id) is completed[0]:
                    completed[0].intentional_stop_turn_ended = not new_activity
                if new_activity:
                    newer = await advance_lifecycle(
                        session_id,
                        store,
                        connection_id=stop.scope.runner_connection_id,
                        expected_scope=stop.scope,
                    )
                    if (
                        newer is not None
                        and completed is not None
                        and _runner_relay_tasks.get(session_id) is completed[0]
                    ):
                        completed[0].shutdown_scope = newer
        except Exception:  # noqa: BLE001 — preserve the original Stop delivery failure
            _logger.warning("Could not cancel shutdown evidence for %s", session_id, exc_info=True)


async def matching_shutdown(
    session_id: str,
    store: ConversationStore,
    *,
    runner_id: str | None = None,
    connection_id: str | None = None,
    lost_at_ms: int | None = None,
    scope: SessionScope | None = None,
) -> SessionShutdown | None:
    """Match identity, pre-disconnect ordering, and an unchanged lifecycle."""
    try:
        state = await asyncio.to_thread(store.get_shutdown_state, session_id)
        if state is None or not state.get("intent"):
            return None
        evidence = SessionShutdown.model_validate_json(state["intent"])
        saved = SessionScope.model_validate_json(state["scope"])
    except Exception:  # noqa: BLE001 — missing evidence must not hide an error
        return None
    current = session_scopes.get(session_id, saved)
    loss = lost_at_ms if lost_at_ms is not None else timestamp_ms()
    if (
        not evidence.intent.requested
        or current != evidence.scope
        or saved != evidence.scope
        or (scope is not None and scope != evidence.scope)
        or state["runner_id"] != evidence.scope.runner_id
        or (runner_id is not None and runner_id != evidence.scope.runner_id)
        or (connection_id is not None and connection_id != evidence.scope.runner_connection_id)
        or not 0 <= loss - evidence.recorded_at_ms <= SHUTDOWN_INTENT_TTL_MS
    ):
        return None
    return evidence


async def settle_shutdown(
    session_id: str,
    evidence: SessionShutdown,
    store: ConversationStore,
    *,
    can_publish: Callable[[], bool] | None = None,
) -> bool:
    """Conditionally settle this lifecycle without erasing a genuine failure."""
    from omnigent.server import session_live_state
    from omnigent.server.routes._sessions.common import _session_status_cache
    from omnigent.server.routes._sessions.helpers import (
        _last_task_error_from_labels,
        _publish_status,
    )

    try:
        conv = await asyncio.to_thread(store.get_conversation, session_id)
        if conv is None or conv.runner_id != evidence.scope.runner_id:
            return False
        if session_scopes.get(session_id, evidence.scope) != evidence.scope:
            return False
        if can_publish is not None and not can_publish():
            return False
        preserve = (
            evidence.preserve_failure
            or _session_status_cache.get(session_id, conv.live_status) == "failed"
            or _last_task_error_from_labels(conv.labels) is not None
        )
        await session_live_state.drain_pending_writes()
        if can_publish is not None and not can_publish():
            return False
        serialized_scope = evidence.scope.model_dump_json()
        applied = await asyncio.to_thread(
            store.compare_shutdown_state,
            session_id,
            runner_id=evidence.scope.runner_id,
            expected_scope=serialized_scope,
            scope=serialized_scope,
            intent=evidence.model_dump_json(),
            expected_intent=evidence.model_dump_json(),
            settle=True,
            preserve_failure=preserve,
            intent_id=evidence.intent.shutdown_id,
            intent_recorded_at_ms=evidence.recorded_at_ms,
        )
        if not applied or session_scopes.get(session_id, evidence.scope) != evidence.scope:
            return False
        state = await asyncio.to_thread(store.get_shutdown_state, session_id)
        if state is None or state.get("scope") != serialized_scope:
            return False
        if can_publish is not None and not can_publish():
            return False
        session_live_state.forget_live_status(session_id)
        preserve = (
            preserve
            or _session_status_cache.get(session_id) == "failed"
            or state["live_status"] == "failed"
        )
        if not preserve:
            _publish_status(session_id, "idle", persist_live_status=False)
        if session_shutdowns.get(session_id) == evidence:
            session_shutdowns.pop(session_id, None)
        _logger.info(
            "Runner disconnect attributed to shutdown",
            extra=debug_event(
                "session_shutdown_applied",
                session_id=session_id,
                **evidence.log_attrs(),
                preserve_failure=preserve or state["live_status"] == "failed",
            ),
        )
        return True
    except Exception:  # noqa: BLE001 — missing evidence must fall through to failure reporting
        _logger.warning(
            "Could not settle shutdown evidence for %s",
            session_id,
            extra=debug_event(
                "shutdown_attribution_failed", session_id=session_id, operation="settle"
            ),
            exc_info=True,
        )
        return False
