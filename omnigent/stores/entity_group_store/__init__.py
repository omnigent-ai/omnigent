"""Entity group store — manages named, icon-bearing categories for entities.

An *entity group* organizes entities into categories shown in the flow builder's
step picker (e.g. a user's own "Deploy" group). This store owns user-created
groups only; the built-in Jira/GitHub groups are code-owned (see
:mod:`omnigent.entities.builtins`) and merged in by the route layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import EntityGroup


class EntityGroupStore(ABC):
    """
    Abstract base for entity-group persistence.

    Manages the lifecycle of user-created entity groups (CRUD, scoped per owner).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the entity group store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///entities.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create_group(
        self,
        *,
        name: str,
        icon_key: str | None = None,
        created_by: str | None = None,
    ) -> EntityGroup:
        """
        Create a new group. The store mints the id.

        :param name: Human-readable group name.
        :param icon_key: Optional bundled-icon key (normally unset for user
            groups, which upload an icon instead).
        :param created_by: Owning user id, or ``None`` in single-user mode.
        :returns: The newly created :class:`EntityGroup`.
        """
        ...

    @abstractmethod
    def get_group(self, group_id: str) -> EntityGroup | None:
        """
        Return the group, or ``None`` if it does not exist.

        :param group_id: Unique group identifier, e.g. ``"grp_abc123"``.
        :returns: The :class:`EntityGroup` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list_groups(self, *, created_by: str | None = None) -> list[EntityGroup]:
        """
        List groups, newest-updated first.

        :param created_by: When set, only return groups owned by this user.
            When ``None`` (single-user mode), return all groups.
        :returns: Groups ordered by ``updated_at`` descending.
        """
        ...

    @abstractmethod
    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        icon_key: str | None = None,
        icon_artifact_key: str | None = None,
        icon_content_type: str | None = None,
    ) -> EntityGroup | None:
        """
        Patch the given fields and bump ``updated_at``. Only non-``None``
        arguments are applied. Returns the updated group, or ``None`` if the
        id is unknown.

        :param group_id: Unique group identifier, e.g. ``"grp_abc123"``.
        :returns: The updated :class:`EntityGroup`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def delete_group(self, group_id: str) -> bool:
        """
        Delete a group and null the ``group_id`` of any entities that
        referenced it (so they become ungrouped rather than dangling).

        :param group_id: Unique group identifier, e.g. ``"grp_abc123"``.
        :returns: ``True`` if deleted, ``False`` if it did not exist.
        """
        ...
