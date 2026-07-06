"""PublishedAgent entity — a community-published registry entry."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PublishedAgent:
    """
    A community-published agent in the registry.

    Each instance represents a distinct ``name@version`` publication.
    Multiple versions of the same agent name co-exist as separate entries.

    :param id: Unique publication identifier, e.g. ``"pa_0f1a2b3c..."``.
    :param name: Slug-style agent name, e.g. ``"code-reviewer"``.
    :param version: Semver string, e.g. ``"1.2.0"``.
    :param harness: Executor harness, e.g. ``"claude-sdk"`` or ``"codex"``.
    :param description: Human-readable description of the agent's purpose.
    :param author: Publisher handle or email address.
    :param created_at: Unix epoch seconds when the agent was published.
    :param category: Optional high-level category, e.g. ``"coding"``.
    :param tags: Searchable tags, e.g. ``["rag", "typescript"]``.
    :param prompt_excerpt: First ~200 characters of the system prompt.
    :param network_access: Whether the agent makes outbound network requests.
    :param write_access: Whether the agent writes to the filesystem.
    :param guardrails: Human-readable summary of safety restrictions.
    :param source_url: Link to the agent's source repository or raw YAML.
    :param stars_count: Cumulative star count.
    :param bundle_location: Artifact store key for the downloadable bundle,
        or ``None`` if no bundle has been uploaded.
    :param updated_at: Unix epoch seconds of the last metadata update,
        or ``None`` if never updated after initial publication.
    """

    id: str
    name: str
    version: str
    harness: str
    description: str
    author: str
    created_at: int
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    prompt_excerpt: str | None = None
    network_access: bool = False
    write_access: bool = False
    guardrails: str | None = None
    source_url: str | None = None
    stars_count: int = 0
    bundle_location: str | None = None
    updated_at: int | None = None
