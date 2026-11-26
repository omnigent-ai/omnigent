"""Relaunch native sessions whose runner can't carry project-text edits to them.

A native harness gets its project text (Mandate, persona, Lead charter) at
launch. Runners that report
:data:`~omnigent.runtime.project_instructions_notice.CAP_PROJECT_INSTRUCTION_REFRESH`
bring a later edit into the session's next prompt; older runners never do, so
their long-lived sessions keep acting on the old text however often the server
restarts. When that text changes, each affected session on such a runner gets
the Reload tools path: once it and its runner are idle, the runner is replaced
and the session relaunches, with its conversation, on the current text.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.runtime.project_instructions_notice import CAP_PROJECT_INSTRUCTION_REFRESH

_logger = logging.getLogger(__name__)
_RUNNER_PAGE = 200


@dataclass(frozen=True)
class TextChange:
    """Whose project text changed.

    :param identity_id: Only that role's sessions, after a persona edit.
    :param lead: Only the Lead's sessions, after a charter edit.
    Neither means every session in the project, after a Mandate edit.
    """

    identity_id: str | None = None
    lead: bool = False


def runner_lacks_refresh(tunnel_registry: Any, runner_id: str) -> bool:
    """Whether *runner_id* is connected and predates in-prompt project-text updates."""
    session = tunnel_registry.get(runner_id) if tunnel_registry is not None else None
    if session is None:
        return False
    return CAP_PROJECT_INSTRUCTION_REFRESH not in (session.hello.capabilities or [])


def _affected(conv: Any, project: Any, change: TextChange) -> bool:
    from omnigent.server.routes._sessions.orchestration import _is_native_terminal_session

    if conv is None or conv.archived or conv.project_id != project.id:
        return False
    if not _is_native_terminal_session(conv):
        return False
    if change.lead:
        return conv.id in {project.main_session_id, *project.retired_main_session_ids}
    if change.identity_id is not None:
        return (conv.labels or {}).get(PROJECT_IDENTITY_LABEL_KEY) == change.identity_id
    return True


async def _runner_session_ids(conversations: Any, runner_id: str) -> list[str]:
    """Every session bound to *runner_id*, page by page."""
    ids: list[str] = []
    after: str | None = None
    while True:
        page = await asyncio.to_thread(
            conversations.list_runner_session_statuses, runner_id, after=after, limit=_RUNNER_PAGE
        )
        ids.extend(session_id for session_id, _status in page)
        if len(page) < _RUNNER_PAGE:
            return ids
        after = page[-1][0]


async def _session_owner(permissions: Any, session_id: str) -> str | None:
    """Who owns *session_id*, so a member's session relaunches as that member."""
    from omnigent.server.auth import LEVEL_OWNER

    if permissions is None:
        return None
    grants = await asyncio.to_thread(permissions.list_for_sessions, [session_id])
    owners = (grant.user_id for grant in grants.get(session_id, []) if grant.level >= LEVEL_OWNER)
    return next(owners, None)


async def recycle_stale_sessions(
    app: Any,
    project: Any,
    change: TextChange,
    *,
    conversations: Any,
    template: Any | None = None,
) -> list[str]:
    """Schedule an idle-time relaunch for each affected session on a pre-refresh runner.

    :param conversations: The conversation store.
    :param template: The editing request, whose sign-in the relaunch reuses.
    :returns: The session ids scheduled, for logging and tests.
    """
    from omnigent.errors import OmnigentError
    from omnigent.server.project_orchestrator_rollover import _owner_request
    from omnigent.server.routes._sessions.tool_reload import (
        require_reloadable,
        schedule_tool_reload,
        tool_reload_pending,
    )

    registry = getattr(app.state, "tunnel_registry", None)
    if registry is None or conversations is None:
        return []
    permissions = getattr(app.state, "permission_store", None)
    scheduled: list[str] = []
    for runner_id in registry.online_runner_ids():
        if not runner_lacks_refresh(registry, runner_id):
            continue
        for session_id in await _runner_session_ids(conversations, runner_id):
            conv = await asyncio.to_thread(conversations.get_conversation, session_id)
            if not _affected(conv, project, change) or tool_reload_pending(session_id):
                continue
            try:
                # A session sharing its parent's runner, or without a host runner, can't reload.
                await require_reloadable(conv, conversations)
            except OmnigentError:
                continue
            schedule_tool_reload(
                _owner_request(app, uuid.uuid4().hex, template),
                session_id,
                user_id=await _session_owner(permissions, session_id) or project.user_id,
                conversation_store=conversations,
                runner_router=getattr(app.state, "runner_router", None),
                permission_store=permissions,
            )
            scheduled.append(session_id)
    if scheduled:
        _logger.info(
            "Project %s text changed: relaunching %d session(s) on runners without "
            "in-prompt updates once idle: %s",
            project.id,
            len(scheduled),
            ", ".join(scheduled),
        )
    return scheduled


def recycle_stale_sessions_soon(
    app: Any,
    project: Any,
    change: TextChange,
    *,
    conversations: Any,
    template: Any | None = None,
) -> None:
    """Fire-and-forget :func:`recycle_stale_sessions`; an edit never waits on or fails for it."""

    async def _run() -> None:
        try:
            await recycle_stale_sessions(
                app, project, change, conversations=conversations, template=template
            )
        except Exception:
            _logger.warning(
                "Could not schedule relaunches for project %s", project.id, exc_info=True
            )

    task = asyncio.create_task(_run(), name=f"recycle-stale-runners:{project.id}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


_TASKS: set[asyncio.Task[None]] = set()
