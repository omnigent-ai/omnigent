"""The project's router: the one persona that receives routed events.

Ticket digests, reliability escalations and reminders, landing escalations, the
``orchestrator`` message alias and Lead heal all ask this module who that is:
the owner's chosen persona, else the built-in Lead, else the first persona.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.server.project_identities import ORCHESTRATOR_IDENTITY_ID

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.entities.project import ProjectIdentity

LEAD_NAME = "Lead"
# The Lead's id in the identity API, mentions and routing settings.
LEAD_IDENTITY_ID = ORCHESTRATOR_IDENTITY_ID
# Names that address the Lead; personas may not take them.
LEAD_ALIASES = frozenset({"lead", "orchestrator"})


@dataclass(frozen=True)
class RouterChoice:
    """Who receives a project's routed events.

    :param identity_id: The persona's id, or :data:`LEAD_IDENTITY_ID` for the Lead.
    :param name: Its display name, e.g. ``"Lead"`` or ``"PM"``.
    :param session_id: Its current session as recorded, which may be gone.
    """

    identity_id: str
    name: str
    session_id: str | None

    @property
    def is_lead(self) -> bool:
        return self.identity_id == LEAD_IDENTITY_ID


def lead_present(project: Project, identities: Sequence[ProjectIdentity]) -> bool:
    """Whether the project has its Lead: always when it has no other persona."""
    return not getattr(project, "lead_removed", False) or not identities


def choose_router(project: Project, identities: Sequence[ProjectIdentity]) -> RouterChoice:
    """Resolve the router from the project's settings and personas, oldest first.

    The configured persona wins while it exists; then the Lead, unless removed;
    then the first persona. A project with no persona always routes to the Lead.
    """
    lead = RouterChoice(LEAD_IDENTITY_ID, LEAD_NAME, project.main_session_id)
    has_lead = lead_present(project, identities)
    configured = getattr(project, "router_identity_id", None)
    if configured == LEAD_IDENTITY_ID and has_lead:
        return lead
    for identity in identities:
        if identity.id == configured:
            return RouterChoice(identity.id, identity.name, identity.active_session_id)
    if has_lead:
        return lead
    first = identities[0]
    return RouterChoice(first.id, first.name, first.active_session_id)


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
