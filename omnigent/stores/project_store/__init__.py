"""Project store — persists first-class, owner-private projects.

A project is a user-defined container that groups sessions and exists
independently of its members (see ``designs/PROJECTS_PRD.md``). This store owns
the ``projects`` table. Session→project membership lives on the conversation's
metadata row (``project_id``) and is managed by the conversation store, not
here.

Every method is scoped by ``user_id``: a caller sees and edits projects they
own or that the owner shared with them (``project_shares``). Deleting a project
and changing share roles stay owner-only; a Manager may invite and remove
ordinary members.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, TypedDict, TypeVar

from omnigent.entities import Project
from omnigent.entities.project import MandateProposal, ProjectIdentity, ProjectShare


class ProjectOrderPreference(TypedDict):
    sort_mode: Literal["alphabetical", "manual"]
    ordered_project_ids: list[str] | None


class ProjectStore(ABC):
    """
    Abstract base for project persistence.

    Manages the lifecycle of projects (CRUD). All reads and writes are scoped
    by ``user_id`` because projects are owner-private.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the project store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        project_id: str,
        name: str,
        user_id: str | None,
        config: dict[str, Any] | None = None,
        mandate: str | None = None,
        description: str | None = None,
    ) -> Project:
        """
        Insert a new, empty project.

        :param project_id: Pre-generated unique project id (a UUID string).
        :param name: Human-readable project name. Trimmed, non-empty, unique
            among the owner's projects.
        :param user_id: Owning user, or ``None`` in single-user mode.
        :param config: Optional default session settings (opaque JSON object);
            ``None`` or empty stores no defaults.
        :param mandate: Optional guidance loaded into every project session;
            trimmed, and blank stores none.
        :param description: Optional charter for people reading the project;
            trimmed, and blank stores none.
        :returns: The newly created :class:`Project`.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the owner already has a
            project with this name.
        """
        ...

    @abstractmethod
    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """
        Return an owned project by id, or ``None`` if not found.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner; a project owned by someone
            else is treated as not found.
        :returns: The :class:`Project` if found and owned, else ``None``.
        """
        ...

    @abstractmethod
    def list(self, *, user_id: str | None) -> list[Project]:
        """
        List the owner's projects ordered by ``created_at ASC, id ASC``.

        :param user_id: The owner whose projects to return.
        :returns: List of :class:`Project` instances.
        """
        ...

    @abstractmethod
    def list_page(
        self, *, user_id: str | None, limit: int, after_id: str | None = None
    ) -> tuple[builtins.list[Project], bool]:
        """
        Return one page of the owner's projects in ``list`` order.

        :param user_id: The owner whose projects to return.
        :param limit: Maximum projects to return, at least 1.
        :param after_id: Continue after this project, the previous page's last id.
        :returns: ``(projects, has_more)``.
        :raises ValueError: If ``after_id`` is not one of the owner's projects.
        """
        ...

    @abstractmethod
    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        main_session_id: str | None = None,
        expected_main_session_id: str | None = None,
        mandate: str | None = None,
    ) -> Project | None:
        """
        Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :param name: New name, or ``None`` to leave unchanged. Trimmed,
            non-empty, unique among the owner's projects.
        :param config: New config object to replace the stored one, or ``None``
            to leave it unchanged. An empty dict clears the stored defaults.
        :param mandate: New mandate text, or ``None`` to leave it unchanged.
            Trimmed; blank text clears it.
        :returns: The updated :class:`Project`, or ``None`` if not found.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the new name collides with
            another of the owner's projects.
        """
        ...

    @abstractmethod
    def delete(
        self,
        project_id: str,
        *,
        user_id: str | None,
        require_no_main: bool = False,
        delete_tickets: bool = False,
    ) -> bool:
        """
        Delete an owned project. Idempotent.

        Deleting a project does not delete its member sessions; unfiling them
        (clearing ``project_id``) is the caller's responsibility.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :param delete_tickets: Also delete the project's tickets and their
            history. Without it, a project with tickets is refused (409).
        :returns: ``True`` if removed; ``False`` if not found / not owned.
        """
        ...

    def list_main_sessions(self) -> builtins.list[tuple[str, str | None, str]]:
        """Every project in the workspace with a main session: ``(project, owner, session)``."""
        raise NotImplementedError

    def set_main_session(
        self,
        project_id: str,
        session_id: str,
        *,
        user_id: str | None,
        expected_main_session_id: str | None = None,
    ) -> Project:
        """Compare-and-set the main session; a repeated assignment is idempotent."""
        raise NotImplementedError

    def set_routing(
        self,
        project_id: str,
        *,
        user_id: str | None,
        router_identity_id: str | None,
        lead_removed: bool,
    ) -> Project:
        """Set the project's router persona and whether its Lead was removed (owner-only)."""
        raise NotImplementedError

    def set_lead_charter(self, project_id: str, *, user_id: str | None, charter: str) -> Project:
        """Set the owner's charter for the built-in Lead; blank clears it (owner-only)."""
        raise NotImplementedError

    def set_description(
        self, project_id: str, *, user_id: str | None, description: str
    ) -> Project:
        """Set the project's description for people; blank clears it (owner-only)."""
        raise NotImplementedError

    def set_lead_session_defaults(
        self, project_id: str, *, user_id: str | None, values: dict[str, str | None]
    ) -> Project | None:
        """Set the Lead's agent, model or effort (project defaults); blank clears one."""
        raise NotImplementedError

    def propose_mandate_addition(
        self,
        project_id: str,
        text: str,
        *,
        user_id: str | None,
        session_id: str | None = None,
        mode: str = "append",
        section: str | None = None,
    ) -> tuple[Project, MandateProposal] | None:
        """
        Queue a mandate change; it reaches no instructions until accepted.

        :param project_id: The owned project.
        :param text: The addition (at most 2,000 UTF-8 bytes), the section's
            replacement (blank deletes it) or the full new mandate.
        :param mode: ``"append"``, ``"replace_section"`` or ``"replace_all"``.
        :param section: The heading title ``replace_section`` targets.
        :param user_id: The requesting owner.
        :param session_id: The proposing session, or ``None`` for the owner.
        :returns: The project and new proposal, or ``None`` if not found / not owned.
        :raises OmnigentError: ``INVALID_INPUT`` for blank or oversized text,
            ``CONFLICT`` when the pending list is full.
        """
        raise NotImplementedError

    def resolve_mandate_proposal(
        self, project_id: str, proposal_id: str, *, user_id: str | None, accept: bool
    ) -> Project | None:
        """
        Accept a pending proposal, appending it to the mandate, or dismiss it.

        :returns: The updated project, or ``None`` if not found / not owned.
        :raises OmnigentError: ``NOT_FOUND`` for an unknown proposal,
            ``INVALID_INPUT`` when accepting would exceed the mandate limit.
        """
        raise NotImplementedError

    # -- Shares: users the project is shared with, as members or Managers --

    def list_shares(self, project_id: str, *, user_id: str | None) -> builtins.list[ProjectShare]:
        """List the users a project is shared with; readable by any member."""
        raise NotImplementedError

    def add_share(
        self, project_id: str, member_id: str, *, user_id: str | None, role: str = "member"
    ) -> ProjectShare:
        """Share a project with *member_id* as *role*.

        The owner may add a member or a Manager, and re-adding an existing share
        sets its role. A Manager may add ordinary members only; re-adding an
        existing share changes nothing. Anyone else is refused.
        """
        raise NotImplementedError

    def set_share_role(
        self, project_id: str, member_id: str, role: str, *, user_id: str | None
    ) -> ProjectShare:
        """Change an existing share's role. Owner-only; ``NOT_FOUND`` when absent."""
        raise NotImplementedError

    def remove_share(self, project_id: str, member_id: str, *, user_id: str | None) -> bool:
        """Revoke *member_id*'s share; ``False`` when absent.

        The owner may remove anyone; a Manager may remove ordinary members only.
        """
        raise NotImplementedError

    # -- Identities: named persistent roles, each with one active session --

    def list_identities(
        self, project_id: str, *, user_id: str | None
    ) -> builtins.list[ProjectIdentity]:
        """List an owned project's identities, oldest first; empty if not owned."""
        raise NotImplementedError

    def get_identity(
        self, project_id: str, identity_id: str, *, user_id: str | None
    ) -> ProjectIdentity | None:
        """Return one identity of an owned project, or ``None``."""
        raise NotImplementedError

    def create_identity(
        self,
        project_id: str,
        identity_id: str,
        *,
        user_id: str | None,
        name: str,
        instructions: str = "",
        agent_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        created_by_session_id: str | None = None,
        scope: Mapping[str, Sequence[str]] | None = None,
    ) -> ProjectIdentity:
        """Insert an identity; names are unique within the project.

        *created_by_session_id* names the creating agent session, or ``None`` for the owner.
        *scope* holds its ``owns`` and ``excludes`` lists.

        :raises OmnigentError: ``NOT_FOUND`` for an unowned project,
            ``ALREADY_EXISTS`` for a duplicate name, ``INVALID_INPUT`` for bounds.
        """
        raise NotImplementedError

    def update_identity(
        self,
        project_id: str,
        identity_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        instructions: str | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        clear: frozenset[str] = frozenset(),
        agent_editor: bool = False,
        scope: Mapping[str, Sequence[str]] | None = None,
    ) -> ProjectIdentity | None:
        """Update fields; ``None`` leaves one unchanged and *clear* names fields to null.

        A *scope* with both lists empty, or ``"scope"`` in *clear*, removes the scope.

        :raises OmnigentError: ``FORBIDDEN`` when *agent_editor* targets an owner-made identity.
        """
        raise NotImplementedError

    def delete_identity(self, project_id: str, identity_id: str, *, user_id: str | None) -> bool:
        """Delete an identity; its sessions remain ordinary project conversations."""
        raise NotImplementedError

    def set_identity_session(
        self,
        project_id: str,
        identity_id: str,
        session_id: str,
        *,
        user_id: str | None,
        expected_session_id: str | None,
    ) -> ProjectIdentity:
        """Compare-and-set the identity's active session; a repeat is idempotent.

        :raises OmnigentError: ``NOT_FOUND`` or ``CONFLICT`` when the pointer moved.
        """
        raise NotImplementedError

    @abstractmethod
    def get_order(self, *, user_id: str | None) -> builtins.list[str] | None:
        """Read saved IDs; None means alphabetical order."""
        raise NotImplementedError

    @abstractmethod
    def get_order_preference(self, *, user_id: str | None) -> ProjectOrderPreference:
        """Read the sorting mode and remembered manual order."""
        raise NotImplementedError

    @abstractmethod
    def save_order(
        self, ids: builtins.list[str] | None, *, user_id: str | None
    ) -> ProjectOrderPreference:
        """Save owned IDs; unranked projects append. Null retains IDs in alphabetical mode."""
        raise NotImplementedError


_T = TypeVar("_T")


def apply_project_order(
    projects: list[_T],
    order: list[str] | None,
    *,
    project_id: Callable[[_T], str | None],
    project_name: Callable[[_T], str],
) -> list[_T]:
    """Order projects, retaining input order for unranked entries in custom mode."""
    if order is None:
        return sorted(projects, key=project_name)
    by_id = {
        project_id(project): project for project in projects if project_id(project) is not None
    }
    ranked = dict.fromkeys(order)
    return [by_id[id] for id in ranked if id in by_id] + [
        project for project in projects if project_id(project) not in ranked
    ]
