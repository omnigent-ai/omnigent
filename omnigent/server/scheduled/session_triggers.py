"""Session triggers: automations that run on Omnigent's own session events.

The server calls :func:`notify` where an event happens; a background worker
matches it to session-triggered tasks and records pending runs, which the host
owner's scheduler claims like Run now requests. Recorded runs survive a replica
restart; queued notifications are best effort. Behavior, guards and limits are
described under "Session triggers" in ``README.md``.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import debug_event
from omnigent.entities import Conversation, ScheduledTask
from omnigent.server.scheduled.schedule import event_trigger_key, occurrence_host_id
from omnigent.stores.conversation_store import (
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

SESSION_TRIGGER_PROVIDER = "omnigent"

SessionEventType = Literal[
    "session.created",
    "session.renamed",
    "session.archived",
    "session.deleted",
    "turn.completed",
    "turn.failed",
    "input.requested",
    "comment.created",
]
SESSION_EVENT_TYPES: tuple[str, ...] = get_args(SessionEventType)

# A loop between automations, or a burst of turns, stops at this many accepted events a minute.
MAX_EVENT_RUNS_PER_MINUTE = 10
# Tasks read per query while listing a workspace's session-triggered tasks.
_TASK_PAGE_SIZE = 500
# Longer occurrence IDs are hashed so the occurrence key fits its column.
_MAX_OCCURRENCE_CHARS = 128
# Longer IDs passed to a run are replaced by their hash, keeping its event document small.
_MAX_ID_CHARS = 256
# Events waiting for the worker; past this, new events are dropped and logged.
_MAX_QUEUED_EVENTS = 1_000
# How long a replica reuses its list of a workspace's session-triggered tasks.
# A task created on another replica starts receiving events within this time.
_TASK_LIST_TTL_SECONDS = 15.0


class SessionTriggerConfig(BaseModel):
    """The ``trigger_config`` of a session-triggered task.

    :param provider: Always ``"omnigent"``.
    :param events: The event types that fire the task, e.g. ``["turn.completed"]``.
    :param agent_id: Fire only for sessions of this agent.
    :param project_id: Fire only for sessions filed in this project.
    :param title_contains: Fire only for sessions whose title contains this text,
        ignoring case.
    :param include_own_sessions: Also fire for sessions this task created or
        continues, and their sub-agents. Off by default, so a task's own turns
        cannot trigger it again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["omnigent"]
    events: list[SessionEventType] = Field(min_length=1)
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    title_contains: str | None = Field(default=None, min_length=1, max_length=256)
    include_own_sessions: bool = False

    @field_validator("events")
    @classmethod
    def _dedupe_events(cls, events: list[SessionEventType]) -> list[SessionEventType]:
        return sorted(set(events))

    def matches(self, event_type: str, conversation: Conversation) -> bool:
        """Whether the event type and the session pass this trigger's filters."""
        if event_type not in self.events:
            return False
        if self.agent_id is not None and conversation.agent_id != self.agent_id:
            return False
        if self.project_id is not None and conversation.project_id != self.project_id:
            return False
        if self.title_contains is not None:
            title = (conversation.title or "").casefold()
            if self.title_contains.casefold() not in title:
                return False
        return True


def parse_session_trigger(trigger_config: dict[str, Any] | None) -> SessionTriggerConfig | None:
    """The task's session trigger, or ``None`` for another provider or an unreadable one."""
    if trigger_config is None or trigger_config.get("provider") != SESSION_TRIGGER_PROVIDER:
        return None
    try:
        return SessionTriggerConfig.model_validate(trigger_config)
    except ValidationError:
        _logger.warning("Ignoring a session trigger that no longer validates", exc_info=True)
        return None


@dataclass(frozen=True)
class SessionEvent:
    """One session event, before it is matched to tasks.

    :param type: One of :data:`SESSION_EVENT_TYPES`.
    :param session_id: The session the event happened on.
    :param occurrence: What makes this event distinct from others of its type on
        the session, e.g. the response, elicitation or comment ID. Part of the
        occurrence key, so the same occurrence never records two runs.
    :param occurred_at: Unix epoch seconds.
    :param ids: Further IDs to pass to the run, e.g. ``{"response_id": "resp_1"}``.
    """

    type: str
    session_id: str
    occurrence: str
    occurred_at: int
    ids: tuple[tuple[str, str], ...] = ()

    @property
    def trigger_key(self) -> str:
        occurrence = self.occurrence
        if len(occurrence) > _MAX_OCCURRENCE_CHARS:
            # Harness-assigned IDs have no length bound; the key column does.
            occurrence = hashlib.sha256(occurrence.encode()).hexdigest()
        return event_trigger_key(
            SESSION_TRIGGER_PROVIDER, f"{self.session_id}:{self.type}:{occurrence}"
        )

    def document(self, conversation: Conversation) -> dict[str, Any]:
        """The run's description of this event: IDs and metadata, no session content."""
        doc: dict[str, Any] = {
            "provider": SESSION_TRIGGER_PROVIDER,
            "type": self.type,
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            **dict(self.ids),
        }
        for key, value in (
            ("agent_id", conversation.agent_id),
            ("project_id", conversation.project_id),
            ("parent_session_id", conversation.parent_conversation_id),
        ):
            if value is not None:
                doc[key] = value
        return doc


@dataclass
class _Wiring:
    scheduled_task_store: ScheduledTaskStore
    conversation_store: ConversationStore
    permission_store: PermissionStore | None


_wiring: _Wiring | None = None
_executor: ThreadPoolExecutor | None = None
# (loaded_at, tasks) per workspace; the workspace scope keys it.
_task_lists: WorkspaceScopedCache[str, tuple[float, list[ScheduledTask]]] = WorkspaceScopedCache()
_TASK_LIST_KEY = "session_triggered_tasks"


def configure(
    scheduled_task_store: ScheduledTaskStore | None,
    conversation_store: ConversationStore | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Turn session triggers on with the server's stores, or off with ``None``.

    Only the database scheduler claims the pending runs this records, so the
    server configures it only when that scheduler runs.
    """
    global _wiring
    _task_lists.clear()
    if scheduled_task_store is None or conversation_store is None:
        _wiring = None
        return
    _wiring = _Wiring(scheduled_task_store, conversation_store, permission_store)


def enabled() -> bool:
    """Whether this server records session-triggered runs."""
    return _wiring is not None


def invalidate_tasks() -> None:
    """Forget the cached task lists after this replica created or changed a task."""
    _task_lists.clear()


def notify(
    event_type: SessionEventType,
    session_id: str,
    *,
    occurrence: str | None = None,
    conversation: Conversation | None = None,
    readers: frozenset[str] | None = None,
    **ids: str | None,
) -> Future[None] | None:
    """Queue one session event for matching; cheap and safe on any hot path.

    :param event_type: What happened.
    :param session_id: The session it happened on.
    :param occurrence: Distinguishes this event from others of its type on the
        session, e.g. a response ID. ``None`` makes the event unique, so it never
        deduplicates.
    :param conversation: The session as the caller already has it. Required for
        ``session.deleted``, since the row is gone by the time the worker runs.
    :param readers: The result of :func:`readers_of`, captured before a deletion
        removed the session's grants. ``None`` checks each task owner's grant.
    :param ids: Extra IDs for the run, e.g. ``response_id="resp_1"``. ``None``
        values are dropped.
    :returns: The queued work, for tests, or ``None`` when triggers are off or
        the queue is full.
    """
    if _wiring is None:
        return None
    now = time.time()
    event = SessionEvent(
        type=event_type,
        session_id=session_id,
        occurrence=occurrence or uuid.uuid4().hex,
        occurred_at=int(now),
        ids=tuple(
            (key, _bounded_id(value)) for key, value in sorted(ids.items()) if value is not None
        ),
    )
    return _submit(_record, event, conversation, readers)


def observe_elicitation(conversation_id: str, event: dict[str, Any]) -> None:
    """Report an approval or question request published on *conversation_id*'s stream.

    A child's request is mirrored onto its ancestors' streams with the child as
    ``params.target_session_id``; only the session that asked reports it.
    """
    elicitation_id = event.get("elicitation_id")
    if event.get("type") != "response.elicitation_request" or not isinstance(elicitation_id, str):
        return
    params = event.get("params")
    target = params.get("target_session_id") if isinstance(params, dict) else None
    if target not in (None, conversation_id):
        return
    notify(
        "input.requested",
        conversation_id,
        occurrence=elicitation_id,
        elicitation_id=elicitation_id,
    )


def readers_of(conversation: Conversation, event_type: SessionEventType) -> frozenset[str] | None:
    """Owners of tasks listening for *event_type* who hold a grant on the session.

    Blocking. A route calls it before a deletion removes the session's grants and
    passes the result to :func:`notify`. Returns ``None`` without reading when
    triggers are off or the server has no permissions.
    """
    wiring = _wiring
    if wiring is None or wiring.permission_store is None:
        return None
    owners = {
        task.user_id
        for task in _session_triggered_tasks(wiring)
        if task.user_id is not None
        and (config := parse_session_trigger(task.trigger_config)) is not None
        and event_type in config.events
    }
    root_id = _access_root_id(conversation)
    return frozenset(owner for owner in owners if _holds_grant(wiring, owner, root_id))


def _holds_grant(wiring: _Wiring, user_id: str, session_id: str) -> bool:
    """Whether *user_id* has its own grant on the session: one indexed row."""
    assert wiring.permission_store is not None
    return wiring.permission_store.get(user_id, session_id) is not None


def _bounded_id(value: str) -> str:
    """An ID as given, or its hash when it is too long to pass to a run."""
    if len(value) <= _MAX_ID_CHARS:
        return value
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


_queued = 0
_queued_lock = threading.Lock()


def _submit(fn: Callable[..., None], *args: object) -> Future[None] | None:
    global _executor, _queued
    with _queued_lock:
        if _queued >= _MAX_QUEUED_EVENTS:
            _logger.warning("Dropped a session event: %d are already waiting", _queued)
            return None
        _queued += 1
        if _executor is None:
            # One worker keeps events of a session in the order they happened.
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-triggers")
        executor = _executor
    # The worker runs in the caller's workspace scope, as every store read filters on it.
    ctx = contextvars.copy_context()

    def run() -> None:
        global _queued
        try:
            fn(*args)
        except Exception:
            _logger.exception("Could not record session-triggered runs")
        finally:
            with _queued_lock:
                _queued -= 1

    return executor.submit(ctx.run, run)


def _session_triggered_tasks(wiring: _Wiring) -> list[ScheduledTask]:
    cached = _task_lists.get(_TASK_LIST_KEY)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _TASK_LIST_TTL_SECONDS:
        return cached[1]
    tasks: list[ScheduledTask] = []
    after: str | None = None
    while True:
        page = wiring.scheduled_task_store.list_event_triggered_tasks(
            provider=SESSION_TRIGGER_PROVIDER, after=after, limit=_TASK_PAGE_SIZE
        )
        tasks.extend(page)
        if len(page) < _TASK_PAGE_SIZE:
            break
        after = page[-1].id
    _task_lists[_TASK_LIST_KEY] = (now, tasks)
    return tasks


def _access_root_id(conversation: Conversation) -> str:
    """The session whose grants cover *conversation*: itself, or its root for a sub-agent."""
    if conversation.parent_conversation_id is None:
        return conversation.id
    return conversation.root_conversation_id or conversation.parent_conversation_id


def _is_own_session(task: ScheduledTask, conversation: Conversation, root: Conversation) -> bool:
    """Whether the task created or continues the session, or the session's root."""
    return any(
        session.labels.get(SCHEDULED_TASK_ORIGIN_LABEL_KEY) == task.id
        or session.id == task.session_id
        for session in (conversation, root)
    )


def _record(
    event: SessionEvent, conversation: Conversation | None, readers: frozenset[str] | None
) -> None:
    wiring = _wiring
    if wiring is None:
        return
    matched = [
        (task, config)
        for task in _session_triggered_tasks(wiring)
        if (config := parse_session_trigger(task.trigger_config)) is not None
        and event.type in config.events
    ]
    if not matched:
        return
    conv = conversation or wiring.conversation_store.get_conversation(event.session_id)
    if conv is None or conv.agent_id is None:
        # Not a session, or deleted before the worker read it.
        return
    root_id = _access_root_id(conv)
    root = conv
    if root_id != conv.id:
        root = wiring.conversation_store.get_conversation(root_id) or conv
    document = event.document(conv)
    now = int(time.time())
    for task, config in matched:
        if not config.matches(event.type, conv):
            continue
        if not config.include_own_sessions and _is_own_session(task, conv, root):
            continue
        # Single-user servers have no grants: every session is the owner's.
        if task.user_id is not None and wiring.permission_store is not None:
            if readers is not None:
                if task.user_id not in readers:
                    continue
            elif not _holds_grant(wiring, task.user_id, root_id):
                continue
        trigger_key = event.trigger_key
        outcome = wiring.scheduled_task_store.record_event_run(
            task,
            now=now,
            trigger_key=trigger_key,
            host_id=occurrence_host_id(task, trigger_key),
            event=document,
            coalesce=task.session_mode == "continue",
            max_runs_per_minute=MAX_EVENT_RUNS_PER_MINUTE,
        )
        log = _logger.warning if outcome == "rate_limited" else _logger.info
        log(
            "Session event %s on %s for task %s: %s",
            event.type,
            event.session_id,
            task.id,
            outcome,
            extra=debug_event(
                "scheduled_event_run_recorded",
                task_id=task.id,
                task_workspace_id=task.workspace_id,
                trigger_key=trigger_key,
                event_type=event.type,
                session_id=event.session_id,
                outcome=outcome,
            ),
        )


def render_event_prompt(prompt: str, event: dict[str, Any] | None) -> str:
    """Append a run's events to the task's prompt, as IDs and metadata only.

    :param prompt: The task's saved prompt.
    :param event: The run's ``event`` document, or ``None`` for a run that no
        event recorded (a Run now request).
    :returns: The prompt, followed by one JSON line per event.
    """
    events = (event or {}).get("events")
    if not isinstance(events, list) or not events:
        return prompt
    lines = [
        json.dumps({k: v for k, v in e.items() if k != "key"}, sort_keys=True)
        for e in events
        if isinstance(e, dict)
    ]
    omitted = int((event or {}).get("omitted") or 0)
    if omitted:
        lines.append(f"(and {omitted} more events, not listed)")
    noun = "event" if len(lines) == 1 else "events"
    return (
        f"{prompt.rstrip()}\n\n<omnigent-trigger-events>\n"
        f"This run was started by the following session {noun}. They hold IDs only; "
        "use your session tools to read the sessions.\n"
        + "\n".join(lines)
        + "\n</omnigent-trigger-events>"
    )
