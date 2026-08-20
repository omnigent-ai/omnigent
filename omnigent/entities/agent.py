"""Agent entity."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omnigent.spec import AgentSpec


@dataclass
class Agent:
    """
    A registered agent.

    :param id: Unique agent identifier, e.g. ``"ag_abc123"``.
    :param created_at: Unix epoch timestamp of creation.
    :param name: Human-readable agent name, e.g.
        ``"research-agent"``. Template agents have unique names;
        session-scoped copies may reuse names across sessions.
    :param bundle_location: Artifact store key for the current bundle,
        e.g. ``"ag_abc123/a1b2c3d4e5f6..."``. Content-addressed
        (SHA-256 hex of the bundle bytes).
    :param version: Monotonic version counter. Starts at 1, incremented
        on each update.
    :param description: Optional free-text description of the agent.
    :param updated_at: Unix epoch timestamp of the last update, or
        ``None`` if the agent has never been updated.
    """

    id: str
    created_at: int
    name: str
    bundle_location: str
    version: int = 1
    description: str | None = None
    updated_at: int | None = None
    session_id: str | None = None  # owning conversation id; None for template agents


@dataclass
class LoadedAgent:
    """
    A fully loaded agent — parsed spec plus the extracted working
    directory on disk. Returned by ``AgentCache.load()``.

    :param spec: The parsed agent spec from config.yaml.
    :param workdir: Path to the extracted agent image directory on disk.
    """

    spec: AgentSpec
    workdir: Path
