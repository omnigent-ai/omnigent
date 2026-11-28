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

from omnigent.errors import ErrorCode, OmnigentError


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
    :param main_session_id: The project's Lead session, or ``None``.
    :param mandate: Owner-written guidance (markdown) loaded into every agent
        session in the project to set its bounds, or ``None`` when unset.
    :param mandate_proposals: Proposed additions awaiting the owner, oldest first.
    :param retired_main_session_ids: Former lead sessions, oldest first.
        Server-owned: messages addressed to one reach the current lead.
    :param router_identity_id: The owner-chosen persona that receives routed
        events (``"orchestrator"`` for the Lead), or ``None`` for the default.
    :param lead_removed: Whether the owner removed the built-in Lead persona.
    :param lead_charter: The owner's charter for the Lead, added to its session's
        instructions beside the orchestrator role, or ``None`` when unset.
    :param description: The project's charter for people reading it (mission,
        scope, out of scope, how we'll know it's working, starting roster),
        written by the owner or the Lead, or ``None`` when unset. Unlike the
        mandate it reaches no session's instructions.
    :param retired_roles: Snapshots of roles the Lead or owner retired, by identity ID.
        Server-owned, so the owner can restore one.
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
    retired_main_session_ids: list[str] = field(default_factory=list)
    router_identity_id: str | None = None
    lead_removed: bool = False
    lead_charter: str | None = None
    description: str | None = None
    retired_roles: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class MandateProposal:
    """
    A change to a project's mandate that an agent or the owner proposed.

    It reaches no session's instructions until the owner accepts it.

    :param id: Proposal id (bare 32-char hex).
    :param text: The proposed text, e.g. ``"Run the UI tests before review."``:
        an addition, a section's replacement (blank deletes it) or a whole new mandate.
    :param created_at: Unix epoch seconds when it was proposed.
    :param session_id: The session that proposed it, or ``None`` for the owner.
    :param mode: ``"append"`` adds *text* to the end, ``"replace_section"``
        replaces the section headed *section*, ``"replace_all"`` replaces the mandate.
    :param section: The heading title a ``replace_section`` proposal targets, e.g. ``"Stages"``.
    :param base_hash: SHA-256 of the text a replacement was written against, so
        accepting it after that text changed is refused as stale.
    """

    id: str
    text: str
    created_at: int
    session_id: str | None = None
    mode: str = "append"
    section: str | None = None
    base_hash: str | None = None


# Most UTF-8 bytes a project mandate may hold. It is added to every project
# session's instructions (about 8k tokens at this size). The stored column is
# compressed, so the text may exceed it; the compressed envelope must still fit.
PROJECT_MANDATE_MAX_BYTES = 32_000
# Most bytes of the mandate's stored (compressed, possibly encrypted) envelope.
PROJECT_MANDATE_MAX_STORED_BYTES = 16_383
# Most UTF-8 bytes in one proposed addition, and how many may wait for review.
# Replacement proposals may carry up to PROJECT_MANDATE_MAX_BYTES instead.
MANDATE_PROPOSAL_MAX_BYTES = 2_000
MANDATE_PROPOSAL_MODES = ("append", "replace_section", "replace_all")
# Most bytes of the stored (compressed, possibly encrypted) pending-proposals envelope.
MANDATE_PROPOSALS_MAX_STORED_BYTES = 16_383
MANDATE_PROPOSALS_MAX_PENDING = 5
# Most UTF-8 bytes a project description (its charter for people) may hold.
PROJECT_DESCRIPTION_MAX_BYTES = 8_000


def normalize_project_description(description: str) -> str:
    """Trim a project description and bound its UTF-8 size; blank clears it.

    :raises OmnigentError: ``INVALID_INPUT`` above :data:`PROJECT_DESCRIPTION_MAX_BYTES`.
    """
    trimmed = description.strip()
    size = len(trimmed.encode("utf-8"))
    if size > PROJECT_DESCRIPTION_MAX_BYTES:
        raise OmnigentError(
            f"project description too large ({size} bytes; max {PROJECT_DESCRIPTION_MAX_BYTES})",
            code=ErrorCode.INVALID_INPUT,
        )
    return trimmed


# Most UTF-8 bytes an identity's persona may hold; added to its sessions' instructions.
PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES = 8_000
# Most identities one project may define, besides its built-in Orchestrator.
PROJECT_IDENTITY_MAX_COUNT = 20
# Server-owned label naming the identity a session was started for.
PROJECT_IDENTITY_LABEL_KEY = "omnigent.project_identity_id"


#: A role's scope lists: what it owns and what it doesn't.
ROLE_SCOPE_KEYS = ("owns", "excludes")
ROLE_SCOPE_MAX_ITEMS = 12
ROLE_SCOPE_ITEM_MAX_CHARS = 200
#: Stored size of a role's sealed scope; the caps above fit well inside it.
PROJECT_IDENTITY_SCOPE_MAX_BYTES = 16383


@dataclass
class ProjectIdentity:
    """
    A named, persistent role in a project, such as a PM or designer.

    Sessions come and go; the identity keeps its persona and points at the one
    session currently working for it (single-tracked).

    :param id: UUID primary key (bare 32-char hex string).
    :param project_id: The owning project.
    :param name: Display name, unique within the project, e.g. ``"PM"``.
    :param instructions: Persona text appended to its sessions' instructions.
    :param agent_id: Default agent for new sessions, or ``None`` for the project's.
    :param model: Default model override, or ``None``.
    :param effort: Default reasoning effort, or ``None``.
    :param active_session_id: The identity's current session, or ``None``.
    :param created_by_session_id: The agent session that created it, or ``None``
        when the owner did; agents may edit only agent-created identities.
    :param scope: The role's ``{"owns": [...], "excludes": [...]}`` lists, or
        ``None`` when it has none.
    :param project_agent_id: The role's own agent, a project-scoped copy of
        ``agent_id`` (or the project default), or ``None`` until it is made.
    :param created_at: Unix epoch seconds at creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    """

    id: str
    project_id: str
    name: str
    instructions: str = ""
    agent_id: str | None = None
    model: str | None = None
    effort: str | None = None
    active_session_id: str | None = None
    created_by_session_id: str | None = None
    scope: dict[str, list[str]] | None = None
    project_agent_id: str | None = None
    created_at: int = 0
    updated_at: int | None = None


@dataclass(frozen=True)
class ProjectShare:
    """A user the owner shared a project with.

    :param user_id: The member, e.g. ``"alice@example.com"``.
    :param created_at: Unix epoch seconds when the share was added.
    :param created_by: Who added it (the owner or a Manager), or ``None`` in single-user mode.
    :param role: ``"member"``, or ``"manager"`` for one who may invite and remove members.
    """

    user_id: str
    created_at: int
    created_by: str | None
    role: str = "member"
