"""Entity store — manages reusable, named instructions ("entities").

An *entity* is a building block authored in the web UI (e.g. the Jira actions)
that can be wired into a flow (job) as a step. This store owns entity CRUD,
scoped per owner.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import Entity


class EntityStore(ABC):
    """
    Abstract base for entity persistence.

    Manages the lifecycle of saved entities (CRUD, scoped per owner).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the entity store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///entities.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create_entity(
        self,
        *,
        title: str,
        instruction: str,
        created_by: str | None = None,
        group_id: str | None = None,
    ) -> Entity:
        """
        Create a new entity. The store mints the id.

        :param title: Human-readable title.
        :param instruction: Instruction text folded into a flow when used.
        :param created_by: Owning user id, or ``None`` in single-user mode.
        :param group_id: Owning entity group, or ``None`` if ungrouped.
        :returns: The newly created :class:`Entity`.
        """
        ...

    @abstractmethod
    def get_entity(self, entity_id: str) -> Entity | None:
        """
        Return the entity, or ``None`` if it does not exist.

        :param entity_id: Unique entity identifier, e.g. ``"ent_abc123"``.
        :returns: The :class:`Entity` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list_entities(self, *, created_by: str | None = None) -> list[Entity]:
        """
        List entities, newest-updated first.

        :param created_by: When set, only return entities owned by this user.
            When ``None`` (single-user mode), return all entities.
        :returns: Entities ordered by ``updated_at`` descending.
        """
        ...

    @abstractmethod
    def update_entity(
        self,
        entity_id: str,
        *,
        title: str | None = None,
        instruction: str | None = None,
        group_id: str | None = None,
    ) -> Entity | None:
        """
        Patch the given fields and bump ``updated_at``. Only non-``None``
        arguments are applied. Returns the updated entity, or ``None`` if the
        id is unknown.

        :param entity_id: Unique entity identifier, e.g. ``"ent_abc123"``.
        :param group_id: New owning group id. ``None`` leaves it unchanged;
            pass the empty string to clear it (move to ungrouped).
        :returns: The updated :class:`Entity`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def delete_entity(self, entity_id: str) -> bool:
        """
        Delete an entity. Returns ``True`` if it existed, ``False`` otherwise.

        :param entity_id: Unique entity identifier, e.g. ``"ent_abc123"``.
        :returns: ``True`` if deleted, ``False`` if it did not exist.
        """
        ...
