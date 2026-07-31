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
        *,
        git_url: str | None = None,
        git_ref: str | None = None,
        git_subpath: str | None = None,
        git_commit: str | None = None,
        git_host_id: str | None = None,
    ) -> Agent:
        """
        Register a new template agent. Name must be unique among
        template agents and raises if a template with that name
        already exists.

        :param agent_id: Pre-generated unique agent identifier,
            e.g. ``"ag_0f1a2b3c..."``. Caller generates this so
            the bundle location can be computed before persisting.
        :param name: Human-readable agent name. Must be unique
            among template agents, e.g. ``"code-assistant"``.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param description: Optional free-text description of the
            agent's purpose.
        :param git_url: Clone URL when imported from git; ``None`` for
            non-git agents.
        :param git_ref: Tracked branch/ref used during import.
        :param git_subpath: Agent dir within the repo; ``None`` = root.
        :param git_commit: Resolved commit SHA that was bundled.
        :param git_host_id: ID of the host that cloned the repo.
        :returns: The newly created :class:`Agent`.
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
        *,
        git_commit: str | None = None,
        git_host_id: str | None = None,
    ) -> Agent | None:
        """
        Update an agent's bundle location, bump its version, and
        set ``updated_at``. Returns the updated agent, or ``None``
        if no agent with the given ID exists. ``git_commit`` and
        ``git_host_id`` are written only when explicitly provided.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :param bundle_location: New artifact store key for the
            bundle, e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param git_commit: New resolved commit SHA to record on a git
            refresh, or ``None`` to leave unchanged.
        :param git_host_id: Host ID that performed the re-clone, or
            ``None`` to leave unchanged.
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
