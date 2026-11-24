"""The project's router: the one persona that receives routed events.

Ticket digests, reliability escalations and reminders, landing escalations, the
``orchestrator`` message alias and Lead heal all ask this module who that is:
the owner's chosen persona, else the built-in Lead, else the first persona.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from omnigent.entities.project_routing import (
    LEAD_ALIASES,
    LEAD_IDENTITY_ID,
    LEAD_NAME,
    RouterChoice,
    choose_router,
    lead_present,
)

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.entities.project import ProjectIdentity

__all__ = [
    "LEAD_ALIASES",
    "LEAD_IDENTITY_ID",
    "LEAD_NAME",
    "RouterChoice",
    "choose_router",
    "lead_present",
    "project_router",
    "router_session_id",
    "router_session_ids",
]


def _project_store(app: Any) -> Any:
    notifications = getattr(app.state, "project_ticket_notifications", None)
    return getattr(notifications, "projects", None) or getattr(app.state, "project_store", None)


async def project_identities(app: Any, project: Project) -> list[ProjectIdentity]:
    list_identities = getattr(_project_store(app), "list_identities", None)
    if list_identities is None:
        return []
    return await asyncio.to_thread(list_identities, project.id, user_id=project.user_id)


async def project_router(app: Any, project: Project) -> RouterChoice:
    """The project's router, read fresh from its personas."""
    return choose_router(project, await project_identities(app, project))


async def router_session_id(
    app: Any, project: Project, *, restore: bool = True, request: Any | None = None
) -> str | None:
    """The router's session to deliver to, or ``None`` when it has none yet.

    The Lead's is the project's main session (heal and the reliability loop
    replace a lost one). A persona's lost session is restored first when
    *restore* is set, like a mention does.
    """
    choice = await project_router(app, project)
    if choice.is_lead or not restore:
        return choice.session_id
    targets = getattr(app.state, "persona_mention_targets", None)
    if targets is None:
        return choice.session_id
    from omnigent.server.persona_mentions import Mention
    from omnigent.server.project_orchestrator_rollover import _owner_request

    owner_request = _owner_request(app, uuid.uuid4().hex, request)
    try:
        return await targets.open(owner_request, project, Mention(choice.identity_id, choice.name))
    except Exception:
        _logger.warning("Could not restore router %s's session", choice.identity_id, exc_info=True)
        return None


async def router_session_ids(app: Any, project: Project) -> set[str]:
    """Sessions that hear about events as the router: the current one and the Lead's."""
    choice = await project_router(app, project)
    ids = {project.main_session_id, *project.retired_main_session_ids, choice.session_id}
    return {session_id for session_id in ids if session_id}
