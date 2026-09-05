"""Two-tier agent cache — disk + in-memory — backed by ArtifactStore."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from omnigent.entities import LoadedAgent
from omnigent.spec import AgentSpec
from omnigent.spec import load as load_spec
from omnigent.stores.artifact_store import ArtifactStore


@dataclass
class _MutationLockEntry:
    lock: RLock
    users: int = 0


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
        self._mutation_locks_guard = RLock()
        self._mutation_locks: dict[str, _MutationLockEntry] = {}

    @contextmanager
    def _mutation_lock_for(self, agent_id: str) -> Iterator[None]:
        """Serialize one agent while allowing unrelated agents to proceed."""
        with self._mutation_locks_guard:
            entry = self._mutation_locks.get(agent_id)
            if entry is None:
                entry = _MutationLockEntry(lock=RLock())
                self._mutation_locks[agent_id] = entry
            entry.users += 1

        try:
            with entry.lock:
                yield
        finally:
            with self._mutation_locks_guard:
                entry.users -= 1
                if entry.users == 0:
                    self._mutation_locks.pop(agent_id, None)

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
        workdir = self._cache_dir / agent_id

        # Serialize cache reads with mutations so a caller cannot observe a
        # spec while its workdir is being replaced or evicted.
        with self._mutation_lock_for(agent_id):
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

            # Cache miss — download bundle, write to temp file, extract
            bundle_bytes = self._artifact_store.get(bundle_location)
            return self._extract_and_cache(agent_id, bundle_bytes, workdir, expand_env=expand_env)

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

        Extracts the new bundle to a staging directory, atomically swaps it
        into the cache location, publishes the matching in-memory spec, and
        cleans up the old directory. Concurrent readers see one complete
        version and never an empty cache.

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
        with self._mutation_lock_for(agent_id):
            workdir = self._cache_dir / agent_id
            staging_dir = self._reserve_swap_path(agent_id, "staging")
            backup_dir: Path | None = None

            # Extract new bundle to staging directory
            tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz")
            os.close(tmp_fd)
            tmp_path = Path(tmp_name)
            try:
                tmp_path.write_bytes(bundle_bytes)
                spec = load_spec(
                    tmp_path,
                    dest=staging_dir,
                    expand_env=expand_env,
                    prune_invalid_sub_agents=True,
                )
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise
            finally:
                tmp_path.unlink()

            try:
                if workdir.is_dir():
                    backup_dir = self._reserve_swap_path(agent_id, "backup")
                    workdir.rename(backup_dir)
                try:
                    staging_dir.rename(workdir)
                except Exception:
                    if backup_dir is not None and backup_dir.exists():
                        backup_dir.rename(workdir)
                    raise

                # Publish the new spec only after its workdir is in place.
                self._specs[agent_id] = spec
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)

            # A failed cleanup must not undo a completed replacement. A later
            # process cleanup may remove this uniquely named orphan.
            if backup_dir is not None and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)

            return LoadedAgent(spec=spec, workdir=workdir)

    def evict(self, agent_id: str) -> None:
        """
        Remove an agent from both cache tiers. Called when an
        agent is deleted. No-op if the agent is not cached.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        """
        with self._mutation_lock_for(agent_id):
            workdir = self._cache_dir / agent_id
            tombstone_dir: Path | None = None
            if workdir.is_dir():
                tombstone_dir = self._reserve_swap_path(agent_id, "evicted")
                workdir.rename(tombstone_dir)
            self._specs.pop(agent_id, None)
            if tombstone_dir is not None:
                shutil.rmtree(tombstone_dir, ignore_errors=True)

    def _reserve_swap_path(self, agent_id: str, purpose: str) -> Path:
        """Return a unique, currently absent path inside the cache directory."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=f".{agent_id}-{purpose}-", dir=self._cache_dir))
        path.rmdir()
        return path

    def _extract_and_cache(
        self,
        agent_id: str,
        bundle_bytes: bytes,
        workdir: Path,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Extract bundle bytes to disk and populate both cache tiers.

        :param agent_id: Unique agent identifier.
        :param bundle_bytes: Raw bytes of the ``.tar.gz`` bundle.
        :param workdir: Target directory for extraction.
        :param expand_env: Whether to expand ``${VAR}`` references
            against the server process environment. Forwarded from
            :meth:`load`; defaults to ``False`` (fail-safe). See
            :meth:`load` for the rationale.
        :returns: A LoadedAgent with the parsed spec and workdir.
        """
        staging_dir = self._reserve_swap_path(agent_id, "staging")
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(bundle_bytes)
            spec = load_spec(
                tmp_path,
                dest=staging_dir,
                expand_env=expand_env,
                prune_invalid_sub_agents=True,
            )
            staging_dir.rename(workdir)
        except Exception:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        finally:
            tmp_path.unlink()

        self._specs[agent_id] = spec
        return LoadedAgent(spec=spec, workdir=workdir)
