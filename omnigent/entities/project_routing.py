"""Who receives a project's routed events: the owner's choice, the Lead, the first persona.

Pure rules shared by the server's routing paths and the ticket store, which
needs the router to tell its own actions and human-facing comments apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.entities.project import Project, ProjectIdentity

# The Lead's id in the identity API, mentions and routing settings; it keeps its
# original value so stored links and settings stay valid.
LEAD_IDENTITY_ID = "orchestrator"
LEAD_NAME = "Lead"
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
