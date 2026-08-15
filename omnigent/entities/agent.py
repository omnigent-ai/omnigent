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
    # Git-source provenance (all None for non-git agents). git_url None ⇒ not
    # git-backed. git_host_id is the host that cloned it, reused on refresh.
    git_url: str | None = None
    git_ref: str | None = None
    git_subpath: str | None = None
    git_commit: str | None = None
    git_host_id: str | None = None

    @property
    def expands_server_env(self) -> bool:
        """Whether this agent's ``${VAR}`` refs may expand against the server env.

        The single source of truth for the ``expand_env`` decision at every
        spec-load site. Expansion resolves ``${VAR}`` in a spec's MCP
        ``env``/``headers`` and LLM/executor auth against the **server
        process** environment (which holds provider keys, DB creds, etc.), so
        it must be granted only to agent config the operator authored and
        trusts.

        Two provenance classes are therefore denied expansion (fail-safe):

        * **Session-scoped agents** (``session_id`` set) — tenant-supplied
          bundle uploads; expanding their ``${VAR}`` would leak server secrets
          into a spec-controlled MCP/LLM connection.
        * **Git-imported agents** (``git_url`` set) — third-party repo config
          that merely happens to persist as a template (``session_id is
          None``). It is *not* operator-authored, so it gets the same
          no-expansion treatment as a session-scoped upload — matching the
          trust level of ``omni run <cloned-repo>/config.yaml`` locally.

        Only operator-authored template agents (``--agent`` / seeded
        built-ins: ``session_id is None`` **and** ``git_url is None``) expand.
        """
        return self.session_id is None and self.git_url is None


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
