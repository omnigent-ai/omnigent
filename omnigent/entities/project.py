"""Project entity — persisted in the ``projects`` table.

A :class:`Project` is a user-defined, owner-private container that groups
related sessions. It exists independently of its member sessions (it can be
empty), which is why it is a first-class row rather than the implicit
``omni_project`` label it supersedes. Session membership lives on the
conversation's metadata row (``project_id``), not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Project:
    """
    A project persisted in the ``projects`` table.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param name: Human-readable project name, unique per owner.
    :param user_id: User the project belongs to, e.g.
        ``"alice@example.com"``. ``None`` in single-user mode. Ownership is
        stamped on the row (not derived from a permission table) because
        projects are owner-private and carry no ACL of their own.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    :param config: Default session settings as an opaque JSON object (host,
        workspace, harness, model, reasoning effort, git base-branch, …), or an
        empty dict when none are stored. The key vocabulary is owned by the
        client; the store persists and returns it whole. These are hints the
        new-chat dialog pre-fills, not enforced requirements.
    :param main_session_id: The project's Orchestrator session, or ``None``.
    :param mandate: Owner-written guidance (markdown) loaded into every agent
        session in the project to set its bounds, or ``None`` when unset.
    :param mandate_proposals: Proposed additions awaiting the owner, oldest first.
    """

    id: str
    name: str
    user_id: str | None
    created_at: int
    updated_at: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    main_session_id: str | None = None
    mandate: str | None = None
    mandate_proposals: list[MandateProposal] = field(default_factory=list)


@dataclass(frozen=True)
class MandateProposal:
    """
    An addition to a project's mandate that an agent or the owner proposed.

    It reaches no session's instructions until the owner accepts it, which
    appends its text to the mandate.

    :param id: Proposal id (bare 32-char hex).
    :param text: The proposed addition, e.g. ``"Run the UI tests before review."``.
    :param created_at: Unix epoch seconds when it was proposed.
    :param session_id: The session that proposed it, or ``None`` for the owner.
    """

    id: str
    text: str
    created_at: int
    session_id: str | None = None


# Most UTF-8 bytes a project mandate may hold. It is added to every project
# session's instructions, so it stays well inside the stored column's bound.
PROJECT_MANDATE_MAX_BYTES = 12_000
# Most UTF-8 bytes in one proposed addition, and how many may wait for review.
MANDATE_PROPOSAL_MAX_BYTES = 2_000
MANDATE_PROPOSALS_MAX_PENDING = 5
