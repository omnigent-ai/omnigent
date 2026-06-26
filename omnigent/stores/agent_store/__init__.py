"""Agent store — manages registered agents."""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import Agent, PagedList


class AgentStore(ABC):
    """
    Abstract base for agent persistence.

    Manages the lifecycle of registered template agents: creation
    with template-name uniqueness enforcement, lookup by ID or name,
    paginated listing, and deletion.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the agent store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///agents.db"`` for SQLAlchemy or a
            filesystem path for file-backed stores.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None = None,
        owner: str | None = None,
    ) -> Agent:
        """
        Register a new agent. ``owner=None`` registers an operator
        built-in/template agent (names unique among built-ins);
        ``owner`` set registers a standalone user agent (names unique
        per owner). Raises on a name collision within that scope.

        :param agent_id: Pre-generated unique agent identifier,
            e.g. ``"ag_0f1a2b3c..."``. Caller generates this so
            the bundle location can be computed before persisting.
        :param name: Human-readable agent name. Unique among built-ins
            when ``owner`` is ``None``, else unique per owner.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param description: Optional free-text description of the
            agent's purpose.
        :param owner: Owning user id for a standalone agent, e.g.
            ``"alice@example.com"``; ``None`` for an operator built-in.
        :returns: The newly created :class:`Agent`.
        """
        ...

    @abstractmethod
    def list_for_owner(self, owner: str) -> list[Agent]:
        """
        List standalone agents owned by ``owner`` (``session_id IS NULL``
        and ``owner`` matching), newest-first. Excludes operator
        built-ins and session-scoped agents.

        :param owner: Owning user id, e.g. ``"alice@example.com"``.
        :returns: The owner's standalone agents.
        """
        ...

    @abstractmethod
    def get(self, agent_id: str) -> Agent | None:
        """
        Return the agent, or ``None`` if it does not exist.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def get_by_name(self, name: str) -> Agent | None:
        """
        Look up a registered template agent by its unique name.

        :param name: The template agent's unique name,
            e.g. ``"code-assistant"``.
        :returns: The :class:`Agent` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> PagedList[Agent]:
        """
        List registered template agents with cursor-based pagination.

        ``order`` controls the sort direction on ``created_at``
        (``"desc"`` = newest-first, ``"asc"`` = oldest-first).

        :param limit: Maximum number of agents to return.
        :param after: Cursor agent ID; only return agents appearing
            *after* this agent in the sort order,
            e.g. ``"agent_abc123"``.
        :param before: Cursor agent ID; only return agents appearing
            *before* this agent in the sort order.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :returns: A :class:`PagedList` of :class:`Agent` objects.
        """
        ...

    @abstractmethod
    def get_names(self, agent_ids: list[str]) -> dict[str, str]:
        """
        Batch-fetch agent names for a list of IDs.

        Returns a mapping from agent ID to agent name. IDs that do not
        exist in the store are silently omitted from the result.

        :param agent_ids: List of agent identifiers to look up,
            e.g. ``["ag_abc123", "ag_def456"]``.
        :returns: Mapping of ``{agent_id: agent_name}`` for found
            agents.
        """
        ...

    @abstractmethod
    def update(
        self,
        agent_id: str,
        bundle_location: str,
    ) -> Agent | None:
        """
        Update an agent's bundle location, bump its version, and
        set ``updated_at``. Returns the updated agent, or ``None``
        if no agent with the given ID exists.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :param bundle_location: New artifact store key for the
            bundle, e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :returns: The updated :class:`Agent`, or ``None`` if not
            found.
        """
        ...

    @abstractmethod
    def delete(self, agent_id: str) -> bool:
        """
        Delete an agent. Returns ``True`` if the agent existed,
        ``False`` otherwise. Caller is responsible for cancelling
        in-flight tasks before calling this.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :returns: ``True`` if the agent was deleted, ``False`` if
            it did not exist.
        """
        ...
