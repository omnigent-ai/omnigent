"""Project store — persists first-class, owner-private projects.

A project is a user-defined container that groups sessions and exists
independently of its members (see ``designs/PROJECTS_PRD.md``). This store owns
the ``projects`` table. Session→project membership lives on the conversation's
metadata row (``project_id``) and is managed by the conversation store, not
here.

Projects have no ACL of their own (PRD §9): every method is scoped by
``user_id`` so a caller only ever sees and mutates their own projects.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal, TypedDict, TypeVar

from omnigent.entities import Project


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
    ) -> Project:
        """
        Insert a new, empty project.

        :param project_id: Pre-generated unique project id (a UUID string).
        :param name: Human-readable project name. Trimmed, non-empty, unique
            among the owner's projects.
        :param user_id: Owning user, or ``None`` in single-user mode.
        :param config: Optional default session settings (opaque JSON object);
            ``None`` or empty stores no defaults.
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
    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
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
        :returns: The updated :class:`Project`, or ``None`` if not found.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the new name collides with
            another of the owner's projects.
        """
        ...

    @abstractmethod
    def delete(self, project_id: str, *, user_id: str | None) -> bool:
        """
        Delete an owned project. Idempotent.

        Deleting a project does not delete its member sessions; unfiling them
        (clearing ``project_id``) is the caller's responsibility.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :returns: ``True`` if removed; ``False`` if not found / not owned.
        """
        ...

    @abstractmethod
    def get_order(self, *, user_id: str | None) -> builtins.list[str] | None:
        """Read saved IDs; None means alphabetical order."""
        ...

    @abstractmethod
    def get_order_preference(self, *, user_id: str | None) -> ProjectOrderPreference:
        """Read the sorting mode and remembered manual order."""
        ...

    @abstractmethod
    def save_order(
        self, ids: builtins.list[str] | None, *, user_id: str | None
    ) -> ProjectOrderPreference:
        """Save owned IDs, or select alphabetical mode while retaining them."""
        ...


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
    ranked = set(order)
    return [by_id[id] for id in order if id in by_id] + [
        project for project in projects if project_id(project) not in ranked
    ]
