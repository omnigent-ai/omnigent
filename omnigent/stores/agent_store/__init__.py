"""Agent store — manages registered agents."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from dataclasses import dataclass

from omnigent.entities import Agent, PagedList


@dataclass(frozen=True)
class AgentListMetadata:
    """Batch display/load inputs, not an agent ownership authorization record."""

    id: str
    name: str
    bundle_location: str
    kind: str
    created_by: str | None = None

    @property
    def operator_authored(self) -> bool:
        """Tenant agents never expand server env, even when no session owns them."""
        return self.kind == "server" and self.created_by is None


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

    def get_list_metadata(self, agent_ids: builtins.list[str]) -> dict[str, AgentListMetadata]:
        """Read names and bundle provenance in one batch, without ownership lookups."""
        raise NotImplementedError("Agent list metadata is not supported by this store")

    @abstractmethod
    def get_names(self, agent_ids: builtins.list[str]) -> dict[str, str]:
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
        created_by: str | None = None,
    ) -> Agent | None:
        """
        Update an agent's bundle location, bump its version, and
        set ``updated_at``. Returns the updated agent, or ``None``
        if no agent with the given ID exists.

        :param agent_id: Unique agent identifier,
            e.g. ``"agent_abc123"``.
        :param bundle_location: New artifact store key for the
            bundle, e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param created_by: When set, stamps the agent's owner only if
            it is not already recorded (claim-on-write). Used by the
            session-scoped mutation routes to heal pre-migration rows
            whose ``created_by`` is ``None``; template-refresh callers
            leave it ``None`` so template rows stay unowned.
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

    # ── User agents ─────────────────────────────────────────────
    # Agents a user installed or uploaded (kind "user", owned via created_by).
    # Non-abstract with safe defaults, so a store that doesn't implement them
    # keeps working and the server reports user agents as unsupported.

    @property
    def supports_user_agents(self) -> bool:
        """Whether :meth:`create_user_agent` and :meth:`list_user_agents` work."""
        return False

    def create_user_agent(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        owner: str | None,
        description: str | None = None,
    ) -> Agent:
        """
        Insert a user agent owned by *owner*.

        :param agent_id: Id for the new row, e.g. ``"0f1a2b3c..."``.
        :param name: Agent name, e.g. ``"orion"``.
        :param bundle_location: Artifact key, ``"<agent_id>/<sha256>"``.
        :param owner: Owning user id, or ``None`` on an auth-less server.
        :param description: Optional description.
        :returns: The created :class:`Agent`.
        :raises NotImplementedError: When the store doesn't support user agents.
        """
        raise NotImplementedError("this agent store does not support user agents")

    def list_user_agents(
        self,
        owner: str | None,
        limit: int = 50,
        after: str | None = None,
    ) -> PagedList[Agent]:
        """
        List *owner*'s own agents, newest first, each by its own id.

        Returns originals only (a bundle stored under the row's own id; legacy
        fork and switch copies are skipped). Names may repeat: callers address
        an agent by id. Reads a bounded number of rows per call, so a page may
        hold fewer than ``limit`` agents; ``last_id`` is the last row read and
        continues the listing.

        :param owner: Owning user id, or ``None`` on an auth-less server.
        :param limit: Maximum agents to return, e.g. ``50``.
        :param after: ``last_id`` of the previous page.
        :returns: The page; empty when the store doesn't support user agents.
        """
        del owner, limit, after
        return PagedList(data=[], first_id=None, last_id=None, has_more=False)
