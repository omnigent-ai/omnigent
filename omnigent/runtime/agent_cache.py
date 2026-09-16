"""Two-tier agent cache — disk + in-memory — backed by ArtifactStore."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from omnigent.entities import LoadedAgent
from omnigent.spec import AgentSpec
from omnigent.spec import load as load_spec
from omnigent.stores.artifact_store import ArtifactStore


class AgentCache:
    """
    Two-tier cache for loaded agents.

    Tier 1 (in-memory): parsed AgentSpec objects keyed by agent_id.
    Tier 2 (disk): extracted agent directories under cache_dir/<agent_id>/.
    Source of truth: ArtifactStore (tarball bytes).

    On cache miss the bundle is downloaded from the ArtifactStore,
    extracted to disk, parsed, validated, and stored in both tiers.

    This is an **execution** load path, so it loads with
    ``prune_invalid_sub_agents=True``: a sub-agent that fails
    validation here means this server is older than whatever produced
    the bundle and can't run that sub-agent (version skew), so it is
    dropped (with a WARNING) and the parent agent still dispatches.
    Authoring/upload validation stays strict elsewhere
    (:func:`omnigent.server.bundles.validate_agent_bundle`). See
    :func:`omnigent.spec.load`.
    """

    def __init__(self, artifact_store: ArtifactStore, cache_dir: Path) -> None:
        """
        Initialize the two-tier agent cache.

        :param artifact_store: The ArtifactStore holding agent
            bundle tarballs (source of truth).
        :param cache_dir: Root directory for the disk cache.
            Each agent is extracted to
            ``<cache_dir>/<agent_id>/``.
        """
        self._artifact_store = artifact_store
        self._cache_dir = cache_dir
        self._specs: dict[str, AgentSpec] = {}

    def _cache_path(self, agent_id: str) -> Path:
        """Return a direct child of the cache root for an agent id."""
        component = os.path.basename(agent_id)
        if (
            not component
            or component in {".", ".."}
            or component != agent_id
            or "\\" in component
            or "\x00" in component
        ):
            raise ValueError(f"unsafe agent id for cache path: {agent_id!r}")
        cache_root = self._cache_dir.resolve(strict=False)
        normalized_path = os.path.normpath(cache_root / component)
        if not normalized_path.startswith(os.path.join(cache_root, "")):
            raise ValueError(f"unsafe agent id for cache path: {agent_id!r}")
        path = Path(normalized_path)
        if path.parent != cache_root or path.resolve(strict=False) != path:
            raise ValueError(f"unsafe agent id for cache path: {agent_id!r}")
        return path

    def load(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Load an agent, populating caches on miss.

        Raises KeyError if the agent bundle does not exist in the
        ArtifactStore. Raises ValueError if the spec is invalid.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param expand_env: Whether to expand ``${VAR}`` references in
            the spec against the server process environment. Defaults
            to ``False`` and MUST stay ``False`` for tenant-supplied
            (session-scoped) agents: expanding their ``${VAR}``
            against the server env leaks secrets into a spec-controlled
            MCP/LLM connection. Callers pass
            ``expand_env=True`` only for operator-authored template
            agents (``Agent.session_id is None`` — ``--agent`` /
            built-ins). The default is fail-safe: a caller that
            forgets the flag gets no expansion (a template agent may
            fail to resolve, loudly) rather than a silent leak.
        :returns: A LoadedAgent with the parsed spec and the
            on-disk working directory.
        """
        workdir = self._cache_path(agent_id)

        # Tier 1: in-memory spec. The cached spec was parsed with the
        # *expand_env* value of whichever caller populated it first.
        # That is consistent across callers because *expand_env* is
        # derived from the agent's immutable ``session_id`` provenance,
        # which never changes for a given ``agent_id``.
        if agent_id in self._specs:
            return LoadedAgent(spec=self._specs[agent_id], workdir=workdir)

        # Tier 2: disk cache (directory already extracted)
        if workdir.is_dir():
            spec = load_spec(workdir, expand_env=expand_env, prune_invalid_sub_agents=True)
            self._specs[agent_id] = spec
            return LoadedAgent(spec=spec, workdir=workdir)

        # Cache miss — validate privately before publishing the disk entry.
        bundle_bytes = self._artifact_store.get(bundle_location)
        return self._extract_and_cache(agent_id, bundle_bytes, expand_env=expand_env)

    def replace(
        self,
        agent_id: str,
        bundle_location: str,
        bundle_bytes: bytes,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Warm-swap an agent's cached spec and disk directory.

        Validates the new bundle in a temporary directory before
        replacing the disk directory and updating the in-memory spec.
        Callers must coordinate replacement with concurrent use of
        this agent; the disk swap and memory update are not atomic.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        :param bundle_location: New artifact store key (unused
            during extraction but passed for consistency),
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param bundle_bytes: Raw bytes of the new ``.tar.gz``
            bundle.
        :param expand_env: Whether to expand ``${VAR}`` references
            against the server process environment. Defaults to
            ``False`` (fail-safe); pass ``True`` only for
            operator-authored template agents. See :meth:`load` for
            the full rationale.
        :returns: A LoadedAgent with the new spec and working
            directory.
        """
        workdir = self._cache_path(agent_id)
        with self._staging_dir() as staging_dir:
            spec = load_spec(
                bundle_bytes,
                dest=staging_dir,
                expand_env=expand_env,
                prune_invalid_sub_agents=True,
            )
            workdir = self._cache_path(agent_id)
            if workdir.is_dir():
                shutil.rmtree(workdir)
            staging_dir.rename(workdir)
        self._specs[agent_id] = spec
        return LoadedAgent(spec=spec, workdir=workdir)

    def evict(self, agent_id: str) -> None:
        """
        Remove an agent from both cache tiers. Called when an
        agent is deleted. No-op if the agent is not cached.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        """
        workdir = self._cache_path(agent_id)
        self._specs.pop(agent_id, None)
        if workdir.is_dir():
            shutil.rmtree(workdir)

    @contextlib.contextmanager
    def _staging_dir(self) -> Iterator[Path]:
        """Create scratch space outside the live agent-id namespace."""
        cache_root = self._cache_dir.resolve(strict=False)
        cache_root.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(
            tempfile.mkdtemp(prefix=f".{cache_root.name}-staging-", dir=cache_root.parent)
        )
        try:
            yield staging_dir
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def _extract_and_cache(
        self,
        agent_id: str,
        bundle_bytes: bytes,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Extract bundle bytes to disk and populate both cache tiers.

        :param agent_id: Unique agent identifier.
        :param bundle_bytes: Raw bytes of the ``.tar.gz`` bundle.
        :param expand_env: Whether to expand ``${VAR}`` references
            against the server process environment. Forwarded from
            :meth:`load`; defaults to ``False`` (fail-safe). See
            :meth:`load` for the rationale.
        :returns: A LoadedAgent with the parsed spec and workdir.
        """
        with self._staging_dir() as staging_dir:
            spec = load_spec(
                bundle_bytes,
                dest=staging_dir,
                expand_env=expand_env,
                prune_invalid_sub_agents=True,
            )
            workdir = self._cache_path(agent_id)
            try:
                staging_dir.rename(workdir)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                # Another cold loader published first; use its complete bundle.
                workdir = self._cache_path(agent_id)
                if not workdir.is_dir():
                    raise
                spec = load_spec(
                    workdir,
                    expand_env=expand_env,
                    prune_invalid_sub_agents=True,
                )
        self._specs[agent_id] = spec
        return LoadedAgent(spec=spec, workdir=workdir)
