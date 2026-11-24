"""Roll a project's orchestrator over to a fresh conversation seeded with a handoff.

A long-lived orchestrator accumulates thousands of items and slows down. A
rollover waits until the orchestrator is idle, builds a compact handoff (board,
active workers, human asks, recent worker handoffs and owner messages), archives
the old session as read-only history, and starts a new main session through the
ordinary create pipeline. The project pointer moves by compare-and-set only after
the new row exists, and the store remembers every replaced main session so
messages addressed to it reach the current orchestrator.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from starlette.datastructures import Headers
from starlette.requests import Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.util.uuid import normalize_uuid

if TYPE_CHECKING:
    from omnigent.entities import Conversation, Project
    from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

ROLLOVER_EVENT_TYPE = "rollover_orchestrator"
ORCHESTRATOR_ALIAS = "orchestrator"
# Names that address the project's router, the Lead unless the owner chose another.
ROUTER_ALIASES = frozenset({ORCHESTRATOR_ALIAS, "lead"})
SUCCESSOR_LABEL_KEY = "omnigent.project.successor_session"
PREDECESSOR_LABEL_KEY = "omnigent.project.predecessor_session"
ROLLOVER_CONFIG_KEY = "orchestrator_rollover"

_IDLE_POLL_S = 2.0
_IDLE_POLLS = 2
_WAIT_S = 6 * 60 * 60
_SWITCH_WAIT_S = 120.0
_AUTO_CHECK_S = 60.0
_DEFAULT_AUTO_ITEMS = int(os.environ.get("OMNIGENT_ORCHESTRATOR_ROLLOVER_ITEMS", "1500"))
_DEFAULT_AUTO_CONTEXT_RATIO = float(
    os.environ.get("OMNIGENT_ORCHESTRATOR_ROLLOVER_CONTEXT_RATIO", "0.5")
)
_HANDOFF_MAX_CHARS = 24_000
_NOTES_MAX_CHARS = 8_000
_PEER_PREFIX = "[Message from session "
_TICKET_UPDATE_PREFIX = (
    "Automatic ticket update for project ",
    "Ticket update digest for project ",
)

# Old main session id -> pending rollover task (wait for idle, then switch).
_pending: dict[str, asyncio.Task[None]] = {}
# Project id -> old main session id while a rollover is pending or switching.
_project_rollovers: dict[str, str] = {}
# Old main session id -> future resolving to the successor id during the switch.
_switching: dict[str, asyncio.Future[str | None]] = {}


def rollover_in_progress(project_id: str) -> bool:
    """Whether *project_id*'s orchestrator is waiting to roll over or switching now."""
    return project_id in _project_rollovers


def rollover_pending(session_id: str) -> bool:
    task = _pending.get(session_id)
    return task is not None and not task.done()


def _stores(app: Any) -> tuple[Any, Any, Any, Any]:
    notifications = getattr(app.state, "project_ticket_notifications", None)
    if notifications is None:
        raise OmnigentError("Projects are unavailable", code=ErrorCode.RUNNER_UNAVAILABLE)
    return (
        notifications.inputs.conversations,
        notifications.projects,
        notifications.tickets,
        notifications.inputs,
    )


def _rollover_settings(project: Project) -> tuple[bool, int, float]:
    raw = project.config.get(ROLLOVER_CONFIG_KEY)
    raw = raw if isinstance(raw, dict) else {}
    enabled = raw.get("enabled", True) is not False
    items = raw.get("items", _DEFAULT_AUTO_ITEMS)
    ratio = raw.get("context_ratio", _DEFAULT_AUTO_CONTEXT_RATIO)
    items = items if isinstance(items, int) and items > 0 else _DEFAULT_AUTO_ITEMS
    ratio = (
        float(ratio)
        if isinstance(ratio, (int, float)) and 0 < ratio <= 1
        else _DEFAULT_AUTO_CONTEXT_RATIO
    )
    return enabled, items, ratio


# -- Stable address ---------------------------------------------------------


async def resolve_message_target(
    request: Request, target: str, user_id: str | None
) -> tuple[str, str | None]:
    """
    Resolve a ``session_message`` destination to the project's current router.

    ``orchestrator`` or ``lead`` names the sender's project router: its Lead, or
    the persona the owner chose. A session that is rolling over resolves to its
    successor once the switch finishes. A deleted or archived former Lead resolves
    to the project's current Lead, or its router once the Lead was removed.
    Anything else is returned unchanged.

    :returns: ``(session_id, redirected_from)``; ``redirected_from`` is ``None``
        when the target was not rewritten.
    """
    try:
        conversations, projects, _, _ = _stores(request.app)
    except OmnigentError:
        return target, None
    from omnigent.server.project_router import project_router, router_session_id

    if target.strip().lower() in ROUTER_ALIASES:
        source_id = normalize_uuid(request.headers.get(ORCHESTRATION_SOURCE_HEADER) or "")
        source = (
            await asyncio.to_thread(conversations.get_conversation, source_id)
            if source_id
            else None
        )
        if source is None or source.project_id is None:
            raise OmnigentError(
                "`orchestrator` needs a sender that belongs to a project",
                code=ErrorCode.INVALID_INPUT,
            )
        project = await asyncio.to_thread(projects.get, source.project_id, user_id=user_id)
        router = (
            await router_session_id(request.app, project, request=request) if project else None
        )
        if project is None or router is None:
            raise OmnigentError("This project has no lead or router", code=ErrorCode.NOT_FOUND)
        resolved = await _await_switch(router)
        return resolved, ORCHESTRATOR_ALIAS
    normalized = normalize_uuid(target)
    if normalized is None:
        return target, None
    if normalized in _switching:
        resolved = await _await_switch(normalized)
        return resolved, (normalized if resolved != normalized else None)
    conv = await asyncio.to_thread(conversations.get_conversation, normalized)
    if conv is not None and not conv.archived:
        return target, None
    project = await _project_for_retired(projects, normalized, user_id, conv)
    if project is not None and project.lead_removed:
        # The Lead is gone by the owner's choice: its old addresses reach the router.
        if not (await project_router(request.app, project)).is_lead:
            router = await router_session_id(request.app, project, request=request)
            if router is not None and router != normalized:
                return router, normalized
    if project is None or project.main_session_id in (None, normalized):
        if conv is None and project is not None:
            raise OmnigentError(
                "This project's lead was deleted and has no successor yet",
                code=ErrorCode.NOT_FOUND,
            )
        return target, None
    resolved = await _await_switch(project.main_session_id)
    return resolved, normalized


async def _await_switch(session_id: str) -> str:
    future = _switching.get(session_id)
    if future is None:
        return session_id
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        successor = await asyncio.wait_for(asyncio.shield(future), timeout=_SWITCH_WAIT_S)
        if successor:
            return successor
    return session_id


async def _project_for_retired(
    projects: Any, session_id: str, user_id: str | None, conv: Conversation | None
) -> Project | None:
    if conv is not None and conv.project_id is not None:
        project = await asyncio.to_thread(projects.get, conv.project_id, user_id=user_id)
        if project is not None and (
            session_id in project.retired_main_session_ids or project.main_session_id == session_id
        ):
            return project
        return None
    for project in await asyncio.to_thread(projects.list, user_id=user_id):
        if session_id in project.retired_main_session_ids or project.main_session_id == session_id:
            return project
    return None


# -- Authorization -----------------------------------------------------------


async def authorize_rollover(
    request: Any,
    user_id: str | None,
    conv: Conversation,
    permission_store: Any,
) -> Project:
    """
    Check that *conv* is a project's active orchestrator and the caller may roll it.

    The owner may roll over any of their projects' orchestrators. An agent may
    roll over only itself, identified by its bound runner and source header.
    """
    from omnigent.server.session_orchestration import (
        require_owned_session,
        require_private_bound_source,
    )

    conversations, projects, _, _ = _stores(request.app)
    if not user_id and permission_store is None:
        raise OmnigentError("Rolling over requires an owner", code=ErrorCode.FORBIDDEN)
    project = (
        await asyncio.to_thread(projects.get, conv.project_id, user_id=user_id)
        if conv.project_id
        else None
    )
    if project is None or project.main_session_id != conv.id or conv.archived:
        raise OmnigentError(
            "Only a project's active orchestrator can roll over", code=ErrorCode.INVALID_INPUT
        )
    headers = getattr(request, "headers", None)
    source_header = headers.get(ORCHESTRATION_SOURCE_HEADER) if headers is not None else None
    runner_header = headers.get(RUNNER_TUNNEL_TOKEN_HEADER) if headers is not None else None
    if source_header:
        source = await asyncio.to_thread(
            require_private_bound_source, request, user_id, conversations, permission_store
        )
        if source.id != conv.id:
            raise OmnigentError(
                "Only the orchestrator itself or the owner can roll it over",
                code=ErrorCode.FORBIDDEN,
            )
        return project
    if runner_header or not hasattr(request, "url"):
        raise OmnigentError(
            "Runners must name their source session to roll over", code=ErrorCode.FORBIDDEN
        )
    await asyncio.to_thread(
        require_owned_session, conv.id, user_id, conversations, permission_store
    )
    return project


# -- Handoff -----------------------------------------------------------------


def _item_text(item: Any) -> str:
    data = getattr(item, "data", None)
    content = getattr(data, "content", None) or []
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(part for part in parts if part).strip()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if limit <= 300 else text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def build_handoff(
    app: Any, project: Project, old: Conversation, *, notes: str | None, reason: str
) -> tuple[str, str | None]:
    """
    Compose the new orchestrator's first message.

    :returns: ``(text, last_item_id)`` where ``last_item_id`` is the newest item
        of *old* that the handoff accounts for.
    """
    conversations, _, tickets, _ = _stores(app)
    lines: list[str] = [
        f"You are the project orchestrator for project {project.name!r} ({project.id}), "
        f"continuing from the previous orchestrator session {old.id}, which was rolled "
        f"over ({reason}) to give you a fresh context. Load the orchestrator skill and "
        "carry on coordinating. The previous conversation is archived as read-only "
        "history; read it with sys_session_get_history if you need detail. Workers that "
        "still address the old session ID, or the alias `orchestrator`, reach you.",
        "",
    ]
    if notes and notes.strip():
        lines += ["## Notes from the previous orchestrator", _clip(notes, _NOTES_MAX_CHARS), ""]

    board = await asyncio.to_thread(tickets.list, project.id, user_id=project.user_id, limit=100)
    stages = board.get("stages", [])
    complete_id = stages[-1]["id"] if stages else None
    names = {stage["id"]: stage["name"] for stage in stages}
    rows = list(board.get("data", []))
    open_rows = [row for row in rows if row.get("stage_id") != complete_id]
    done_count = len(rows) - len(open_rows)
    lines.append("## Board")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get("stage_id")] = counts.get(row.get("stage_id"), 0) + 1
    lines.append(
        ", ".join(f"{names.get(sid, sid)}: {count}" for sid, count in counts.items())
        + (" (first 100 tickets)" if board.get("has_more") else "")
        if counts
        else "No tickets."
    )
    human_asks: list[str] = []
    workers: list[str] = []
    for row in open_rows:
        labels = ", ".join(
            label["key"] + (f": {label['value']}" if label.get("value") else "")
            for label in row.get("labels", [])
        )
        lines.append(
            f"- `{row['id']}` [{names.get(row.get('stage_id'), row.get('stage_id'))}] "
            f"{_clip(row.get('title', ''), 120)}"
            + (f" — labels: {labels}" if labels else "")
            + (
                f" — latest: {_clip(row['latest_update'], 160)}"
                if row.get("latest_update")
                else ""
            )
        )
        if any(label.get("key") == "needs-human" for label in row.get("labels", [])):
            human_asks.append(f"- `{row['id']}` {_clip(row.get('title', ''), 120)}")
        try:
            linked = await asyncio.to_thread(
                tickets.sessions, project.id, row["id"], user_id=project.user_id, limit=10
            )
        except OmnigentError:
            continue
        for entry in linked.get("data", []):
            sid = entry["session_id"]
            if sid == old.id:
                continue
            session = await asyncio.to_thread(conversations.get_conversation, sid)
            if session is None or session.archived:
                continue
            workers.append(
                f"- `{sid}` ({session.live_status or 'idle'}) on `{row['id'][:8]}` — "
                f"{_clip(entry.get('involvement') or session.title or '', 140)}"
            )
    if done_count:
        lines.append(f"Completed tickets: {done_count}.")
    lines += ["", "## Active workers"] + (workers or ["None linked to open tickets."])
    lines += ["", "## Open human requests (needs-human)"] + (human_asks or ["None."])

    page = await asyncio.to_thread(
        conversations.list_items, old.id, limit=300, order="desc", type="message"
    )
    items = list(getattr(page, "data", page) or [])
    last_item_id = items[0].id if items else None
    handoffs: list[str] = []
    owner: list[str] = []
    last_note: str | None = None
    for item in items:
        role = getattr(item.data, "role", None)
        text = _item_text(item)
        if not text:
            continue
        if role == "assistant":
            if last_note is None and len(text) > 40:
                last_note = text
            continue
        if text.startswith((*_TICKET_UPDATE_PREFIX, "[System:")):
            continue
        if text.startswith(_PEER_PREFIX):
            if len(handoffs) < 10:
                handoffs.append(f"- {_clip(text, 600)}")
        elif len(owner) < 6:
            owner.append(f"- {_clip(text, 800)}")
    lines += [
        "",
        "## Recent worker handoffs, newest first (check for branches awaiting landing)",
    ] + (handoffs or ["None."])
    lines += ["", "## Recent owner messages, newest first"] + (owner or ["None."])
    if last_note:
        lines += ["", "## The previous orchestrator's last reply", _clip(last_note, 2500)]
    lines += [
        "",
        "Start by re-reading the board with sys_ticket_list and confirming which branches "
        "are still waiting to land. Don't redo work that the handoffs say is done.",
    ]
    text = "\n".join(lines)
    if len(text) > _HANDOFF_MAX_CHARS:
        text = text[: _HANDOFF_MAX_CHARS - 80] + "\n\n…(handoff truncated; see the old session)"
    return text, last_item_id


# -- Creating the successor --------------------------------------------------


def _owner_request(app: Any, key: str, template: Any | None = None) -> Request:
    """A request carrying the owner's identity but no agent source, for create."""
    dropped = {
        ORCHESTRATION_SOURCE_HEADER.lower(),
        RUNNER_TUNNEL_TOKEN_HEADER.lower(),
        "idempotency-key",
        "content-length",
        "content-type",
    }
    headers: list[tuple[bytes, bytes]] = []
    scope_template = getattr(template, "scope", None) or {}
    for name, value in scope_template.get("headers", []):
        if name.decode("latin-1").lower() not in dropped:
            headers.append((name, value))
    headers.append((b"idempotency-key", key.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": scope_template.get("scheme", "http"),
        "path": "/v1/sessions",
        "raw_path": b"/v1/sessions",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": scope_template.get("client", ("127.0.0.1", 0)),
        "server": scope_template.get("server", ("127.0.0.1", 0)),
        "app": app,
        "state": {},
    }
    for extra in ("user", "auth"):
        if extra in scope_template:
            scope[extra] = scope_template[extra]
    return Request(scope)


async def start_fresh_main_session(
    app: Any,
    project: Project,
    *,
    user_id: str | None,
    expected_main_session_id: str | None,
    first_message: str | None = None,
    reason: str = "heal",
    template_request: Any | None = None,
    previous: Conversation | None = None,
    nonce: str | None = None,
) -> str:
    """
    Start a new main session for *project*, replacing *expected_main_session_id*.

    Uses the ordinary create pipeline, so the pointer moves by compare-and-set
    only after the new row exists. The current main must be absent, missing or
    archived. A fresh idempotency key avoids the deterministic initial-creation ID.
    *first_message* is enqueued ahead of any pending ticket updates.

    :returns: The new session's id.
    :raises OmnigentError: ``CONFLICT`` if the main session changed meanwhile.
    """
    from omnigent.server.project_main_session import (
        default_project_agent_id,
        project_session_payload,
    )

    create = getattr(app.state, "project_session_create", None)
    if create is None:
        raise OmnigentError("Sessions are unavailable", code=ErrorCode.RUNNER_UNAVAILABLE)
    _, _, _, inputs = _stores(app)
    key = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"omnigent:main-rollover:{project.id}:{expected_main_session_id}:{reason}:"
        f"{nonce or uuid.uuid4().hex}",
    ).hex
    request = _owner_request(app, key, template_request)
    base: dict[str, Any] = {
        "project_id": project.id,
        "title": "Lead",
        "agent_id": project.config.get("agent_id") or (previous.agent_id if previous else None),
        "project_main_session": True,
        "expected_main_session_id": expected_main_session_id,
    }
    if previous is not None:
        for field, value in (
            ("model_override", previous.model_override),
            ("reasoning_effort", previous.reasoning_effort),
            ("harness_override", previous.harness_override),
        ):
            if value and field not in project.config:
                base[field] = value
    payload = await project_session_payload(request, project, base)
    if not payload.get("agent_id"):
        payload["agent_id"] = await default_project_agent_id(
            request, getattr(app.state, "agent_store", None), payload.get("host_id")
        )

    async def queue_first_message(new_id: str) -> None:
        # Runs after the bind but before launch, so the first turn starts with it
        # even when ticket updates were queued the moment the pointer moved.
        if first_message:
            await _enqueue_message(
                app, inputs, new_id, first_message, user_id, first=True, deliver=False
            )

    created = await create(request, user_id, payload, queue_first_message)
    session_id = getattr(created, "id", None) or getattr(created, "session_id", None)
    if not isinstance(session_id, str):
        raise OmnigentError("Session creation failed", code=ErrorCode.INTERNAL_ERROR)
    if first_message:
        await inputs.deliver_pending(app, session_id)
    return session_id


async def _enqueue_message(
    app: Any,
    inputs: Any,
    session_id: str,
    text: str,
    user_id: str | None,
    *,
    first: bool,
    deliver: bool = True,
) -> None:
    from omnigent.server.schemas import SessionEventInput

    await inputs.enqueue(
        session_id,
        SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": text}]},
        ),
        user_id=user_id,
        headers=Headers(),
        first=first,
    )
    if deliver:
        await inputs.deliver_pending(app, session_id)


# -- Rollover ----------------------------------------------------------------


def schedule_rollover(
    request: Any,
    session_id: str,
    *,
    user_id: str | None,
    notes: str | None = None,
    reason: str = "requested",
    on_done: Callable[[str | None], None] | None = None,
) -> bool:
    """
    Roll *session_id* over once it is idle; return ``False`` if one is already pending.

    The wait lives in this replica's memory, so a server restart drops a rollover
    that has not fired; the automatic trigger re-arms it.
    """
    app = request.app
    if rollover_pending(session_id):
        return False
    conversations, _, _, _ = _stores(app)

    async def _when_idle() -> None:
        from omnigent.server.routes._sessions.tool_reload import _session_settled

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WAIT_S
        idle = 0
        while True:
            conv = await asyncio.to_thread(conversations.get_conversation, session_id)
            if conv is None or conv.archived:
                return
            idle = idle + 1 if _session_settled(conv.id, conv.live_status) else 0
            if idle >= _IDLE_POLLS:
                break
            if loop.time() >= deadline:
                _logger.warning("Orchestrator rollover for %s gave up: never idle", session_id)
                return
            await asyncio.sleep(_IDLE_POLL_S)
        successor = await _rollover_now(request, conv, user_id=user_id, notes=notes, reason=reason)
        if on_done is not None:
            on_done(successor)

    task = asyncio.create_task(_when_idle(), name=f"rollover-orchestrator:{session_id}")
    _pending[session_id] = task

    async def _project_marker() -> None:
        conv = await asyncio.to_thread(conversations.get_conversation, session_id)
        if conv is not None and conv.project_id and not task.done():
            _project_rollovers[conv.project_id] = session_id

    marker = asyncio.create_task(_project_marker())

    def _done(finished: asyncio.Task[None]) -> None:
        if _pending.get(session_id) is finished:
            del _pending[session_id]
        marker.cancel()
        for project_id, old in list(_project_rollovers.items()):
            if old == session_id:
                del _project_rollovers[project_id]
        if not finished.cancelled() and finished.exception() is not None:
            _logger.warning(
                "Orchestrator rollover for %s failed", session_id, exc_info=finished.exception()
            )

    task.add_done_callback(_done)
    return True


async def _rollover_now(
    request: Any,
    old: Conversation,
    *,
    user_id: str | None,
    notes: str | None,
    reason: str,
) -> str | None:
    """Archive *old*, start its successor with a handoff, and forward late messages."""
    from omnigent.server.routes._sessions.orchestration import (
        _cancel_pending_archive_stop,
        _spawn_archive_stop,
    )

    app = request.app
    conversations, projects, _, inputs = _stores(app)
    project = await asyncio.to_thread(projects.get, old.project_id, user_id=user_id)
    if project is None or project.main_session_id != old.id:
        return None
    _project_rollovers[project.id] = old.id
    future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    _switching[old.id] = future
    successor: str | None = None
    try:
        # Archive first: ticket updates then wait for the successor instead of
        # landing in the old session after the handoff snapshot.
        await asyncio.to_thread(conversations.update_conversation, old.id, archived=True)
        try:
            handoff, last_item_id = await build_handoff(
                app, project, old, notes=notes, reason=reason
            )
            successor = await start_fresh_main_session(
                app,
                project,
                user_id=user_id,
                expected_main_session_id=old.id,
                first_message=handoff,
                reason="rollover",
                template_request=request,
                previous=old,
                nonce=last_item_id or uuid.uuid4().hex,
            )
        except BaseException:
            await asyncio.to_thread(conversations.update_conversation, old.id, archived=False)
            raise
        _spawn_archive_stop(
            old.id,
            conversations,
            getattr(app.state, "runner_router", None),
            getattr(app.state, "host_registry", None),
            host_store=getattr(app.state, "host_store", None),
            sandbox_config=getattr(app.state, "sandbox_config", None),
            host_session_recoverer=getattr(app.state, "host_session_recoverer", None),
        )
        await asyncio.to_thread(conversations.set_labels, old.id, {SUCCESSOR_LABEL_KEY: successor})
        await asyncio.to_thread(
            conversations.set_labels, successor, {PREDECESSOR_LABEL_KEY: old.id}
        )
        await _forward_late_messages(app, inputs, conversations, old.id, last_item_id, successor)
        _logger.info("Rolled orchestrator %s over to %s (%s)", old.id, successor, reason)
        from omnigent.server.routes._sessions.helpers import announce_projects_changed

        announce_projects_changed(user_id)
        return successor
    except BaseException:
        _cancel_pending_archive_stop(old.id)
        raise
    finally:
        if not future.done():
            future.set_result(successor)
        _switching.pop(old.id, None)
        _project_rollovers.pop(project.id, None)


async def _forward_late_messages(
    app: Any,
    inputs: Any,
    conversations: ConversationStore,
    old_id: str,
    after_item_id: str | None,
    successor: str,
) -> None:
    """Re-send user messages that reached *old_id* after the handoff snapshot."""
    try:
        page = await asyncio.to_thread(
            conversations.list_items, old_id, limit=50, after=after_item_id, type="message"
        )
    except Exception:
        _logger.warning("Could not read late messages for %s", old_id, exc_info=True)
        return
    for item in list(getattr(page, "data", page) or []):
        if getattr(item.data, "role", None) != "user":
            continue
        text = _item_text(item)
        if not text or text.startswith(_TICKET_UPDATE_PREFIX):
            continue
        await _enqueue_message(
            app,
            inputs,
            successor,
            f"[Forwarded from the previous orchestrator session {old_id}]\n{text}",
            None,
            first=False,
        )


# -- Automatic trigger -------------------------------------------------------


def _over_threshold(conv: Conversation, items: int, ratio: float) -> str | None:
    if (conv.next_position or 0) >= items:
        return f"automatic: {conv.next_position} items ≥ {items}"
    try:
        tokens = int(conv.labels.get("omnigent.last_context_tokens", "0"))
        window = int(conv.labels.get("omnigent.last_context_window", "0"))
    except ValueError:
        return None
    if window > 0 and tokens / window >= ratio:
        return f"automatic: context {tokens}/{window} ≥ {ratio:.0%}"
    return None


async def check_auto_rollover(app: Any) -> list[str]:
    """Schedule a rollover for every orchestrator past its threshold; return their ids."""
    conversations, projects, _, _ = _stores(app)
    scheduled: list[str] = []
    for project_id, user_id, main_id in await asyncio.to_thread(projects.list_main_sessions):
        if rollover_pending(main_id) or project_id in _project_rollovers:
            continue
        project = await asyncio.to_thread(projects.get, project_id, user_id=user_id)
        if project is None or project.main_session_id != main_id:
            continue
        enabled, items, ratio = _rollover_settings(project)
        if not enabled:
            continue
        conv = await asyncio.to_thread(conversations.get_conversation, main_id)
        if conv is None or conv.archived or conv.project_id != project_id:
            continue
        reason = _over_threshold(conv, items, ratio)
        if reason is None:
            continue
        request = _owner_request(app, uuid.uuid4().hex)
        if schedule_rollover(request, main_id, user_id=user_id, reason=reason):
            scheduled.append(main_id)
    return scheduled


def start_auto_rollover(app: Any) -> asyncio.Task[None]:
    async def run() -> None:
        while True:
            await asyncio.sleep(_AUTO_CHECK_S)
            try:
                await check_auto_rollover(app)
            except Exception:
                _logger.exception("Automatic orchestrator rollover check failed")

    return asyncio.create_task(run(), name="orchestrator-auto-rollover")
