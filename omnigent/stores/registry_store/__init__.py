"""Registry store: persists and queries community-published agents."""

from abc import ABC, abstractmethod

from omnigent.entities import PagedList, PublishedAgent


class RegistryStore(ABC):
    """Abstract base for community registry persistence.

    Manages the lifecycle of published agents: creation, retrieval
    by name/version, paginated browse with filters, and star voting.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the registry store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def publish(
        self,
        publication_id: str,
        name: str,
        version: str,
        harness: str,
        description: str,
        author: str,
        created_at: int,
        *,
        category: str | None = None,
        tags: list[str] | None = None,
        prompt_excerpt: str | None = None,
        network_access: bool = False,
        write_access: bool = False,
        guardrails: str | None = None,
        source_url: str | None = None,
        bundle_location: str | None = None,
    ) -> PublishedAgent:
        """Persist a new agent publication.

        :param publication_id: Pre-generated unique id, e.g. ``"pa_abc123"``.
        :param name: Slug-style agent name, e.g. ``"code-reviewer"``.
        :param version: Semver string, e.g. ``"1.2.0"``.
        :param harness: Executor harness, e.g. ``"claude-sdk"``.
        :param description: Human-readable purpose of the agent.
        :param author: Publisher handle or email.
        :param created_at: Unix epoch seconds of publication.
        :param category: Optional category slug, e.g. ``"coding"``.
        :param tags: List of searchable tags.
        :param prompt_excerpt: First ~200 chars of the system prompt.
        :param network_access: Whether the agent calls external URLs.
        :param write_access: Whether the agent writes to the filesystem.
        :param guardrails: Human-readable safety restrictions.
        :param source_url: Link to the agent's source or raw YAML.
        :param bundle_location: Artifact store key for the bundle.
        :returns: The newly created :class:`PublishedAgent`.
        :raises ValueError: If ``name@version`` is already published.
        """
        ...

    @abstractmethod
    def get(self, name: str, version: str) -> PublishedAgent | None:
        """Fetch a specific ``name@version`` publication.

        :param name: Agent name, e.g. ``"code-reviewer"``.
        :param version: Semver string, e.g. ``"1.2.0"``.
        :returns: The :class:`PublishedAgent`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def get_latest(self, name: str) -> PublishedAgent | None:
        """Fetch the most recently published version of an agent by name.

        "Most recently published" means highest ``created_at`` — i.e. the
        last ``publish()`` call for this name, which may not be the highest
        semver. Use :meth:`get` for an exact version lookup.

        :param name: Agent name, e.g. ``"code-reviewer"``.
        :returns: The :class:`PublishedAgent`, or ``None`` if no version
            has been published under this name.
        """
        ...

    @abstractmethod
    def browse(
        self,
        *,
        category: str | None = None,
        harness: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        limit: int = 20,
        after: str | None = None,
    ) -> PagedList[PublishedAgent]:
        """Browse published agents with optional filters and cursor pagination.

        :param category: Filter to this category slug, e.g. ``"coding"``.
        :param harness: Filter to this harness, e.g. ``"claude-sdk"``.
        :param tag: Filter to agents whose tags list includes this value.
        :param q: Keyword search over ``name`` and ``description``.
        :param limit: Maximum number of entries to return (1-1000).
        :param after: Cursor — return entries after this publication id.
        :returns: A :class:`PagedList` of matching :class:`PublishedAgent`
            objects, ordered by ``created_at`` descending.
        """
        ...

    @abstractmethod
    def star(self, name: str, version: str) -> int:
        """Atomically increment the star count for a published agent.

        :param name: Agent name, e.g. ``"code-reviewer"``.
        :param version: Semver string, e.g. ``"1.2.0"``.
        :returns: The new ``stars_count`` after the increment.
        :raises KeyError: If ``name@version`` is not found.
        """
        ...
