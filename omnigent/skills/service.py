"""Fetch a repository's skill catalog and package the skills a user picks.

Clones are short-lived and sparse: the catalog checks out only the ``SKILL.md``
files (and a marketplace manifest) under the source's directory and is cached
in memory per ``(url, ref, path)`` for :data:`CATALOG_TTL_S`; an install
clones again, checking out only the chosen skills' directories, so the
packaged skill always matches the commit it reports.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omnigent.skills.catalog import MARKETPLACE_MANIFEST, CatalogEntry, index_checkout
from omnigent.skills.git_source import (
    GitSourceError,
    SparseCheckout,
    checkout_commit,
    clone_github_source,
    normalize_source_path,
    parse_github_source,
    tar_untrusted_dir,
)
from omnigent.skills.limits import (
    DEFAULT_CLONE_LIMITS,
    MAX_INSTALL_BYTES,
    MAX_SKILL_BYTES,
    MAX_SKILL_ENTRIES,
    CloneLimits,
)
from omnigent.skills.policy import DEFAULT_POLICY, SkillsMarketplacePolicy

CATALOG_TTL_S = 600.0
# Catalogs retained at once; any signed-in user can preview a repository, so
# the cache is bounded as well as expiring.
MAX_CACHED_CATALOGS = 64


class Cloner(Protocol):
    """Clone *clone_url* at *ref* into *dest*; *token* is the operator's, if the policy has one."""

    def __call__(
        self,
        clone_url: str,
        ref: str | None,
        dest: Path,
        *,
        token: str | None = None,
        sparse: SparseCheckout | None = None,
        limits: CloneLimits = DEFAULT_CLONE_LIMITS,
    ) -> None: ...


def server_clone(
    clone_url: str,
    ref: str | None,
    dest: Path,
    *,
    token: str | None = None,
    sparse: SparseCheckout | None = None,
    limits: CloneLimits = DEFAULT_CLONE_LIMITS,
) -> None:
    """The marketplace's clone: isolated from the server's own git credentials and disk-bounded."""
    clone_github_source(
        clone_url,
        ref,
        dest,
        isolated=True,
        token=token,
        max_bytes=limits.checkout_bytes,
        max_entries=limits.checkout_entries,
        sparse=sparse,
        timeout_s=limits.timeout_s,
    )


@dataclass(frozen=True)
class ResolvedSource:
    """A repository, the ref to fetch, and the directory in it to index (``""`` for the root)."""

    url: str
    ref: str | None
    path: str = ""


@dataclass(frozen=True)
class FetchedCatalog:
    """A repository's skills as of one fetch."""

    url: str
    ref: str | None
    commit: str | None
    entries: tuple[CatalogEntry, ...]
    skipped: tuple[str, ...]
    fetched_at: float
    path: str = ""


@dataclass(frozen=True)
class PackagedSkill:
    """One skill ready to store: its catalog entry and its tarball."""

    entry: CatalogEntry
    tarball: bytes


def resolve_source(source: str, ref: str | None = None, path: str | None = None) -> ResolvedSource:
    """Normalize ``owner/repo`` or a GitHub URL into the repository, ref and directory.

    A ``/tree/<ref>/<dir>`` link keeps its directory as the catalog root; an
    explicit *path* wins over it, as an explicit *ref* wins over the link's.

    :raises GitSourceError: When the source is not a GitHub repository or the
        directory is not one inside it.
    """
    parsed = parse_github_source(source.strip())
    if parsed is None:
        raise GitSourceError(
            "Only GitHub repositories are supported: use owner/repo, host/owner/repo, "
            "a github.com URL, or a URL on a GitHub Enterprise host this server knows."
        )
    return ResolvedSource(
        url=parsed.clone_url,
        ref=ref or parsed.ref,
        path=normalize_source_path(path if path is not None else parsed.subpath),
    )


def catalog_patterns(path: str) -> tuple[str, ...]:
    """Sparse patterns for indexing *path*: its ``SKILL.md`` files and marketplace manifest."""
    lead = f"/{path}" if path else ""
    return (f"{lead}/**/SKILL.md", f"{lead}/{MARKETPLACE_MANIFEST}")


class SkillFetcher:
    """Clone, index and package skills from GitHub repositories, within the server's policy."""

    def __init__(
        self,
        *,
        clone: Cloner = server_clone,
        ttl_s: float = CATALOG_TTL_S,
        policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
        max_cached: int = MAX_CACHED_CATALOGS,
    ):
        self._clone = clone
        self._ttl_s = ttl_s
        self._policy = policy
        self._max_cached = max_cached
        self._catalogs: dict[tuple[str, str | None, str], FetchedCatalog] = {}
        self._lock = threading.Lock()

    def _remember(self, key: tuple[str, str | None, str], fetched: FetchedCatalog) -> None:
        """Cache *fetched*, dropping expired entries first and then the oldest past the cap."""
        with self._lock:
            now = time.monotonic()
            for stale in [
                k for k, v in self._catalogs.items() if now - v.fetched_at >= self._ttl_s
            ]:
                del self._catalogs[stale]
            self._catalogs.pop(key, None)
            while len(self._catalogs) >= self._max_cached:
                oldest = min(self._catalogs, key=lambda k: self._catalogs[k].fetched_at)
                del self._catalogs[oldest]
            self._catalogs[key] = fetched

    @property
    def cached_catalogs(self) -> int:
        """How many catalogs are held right now."""
        with self._lock:
            return len(self._catalogs)

    def resolve(
        self, source: str, ref: str | None = None, path: str | None = None
    ) -> ResolvedSource:
        """The repository, ref and directory for a source the policy allows.

        :raises GitSourceError: When the source is not GitHub or the policy refuses it.
        """
        resolved = resolve_source(source, ref, path)
        self._policy.require_allowed(resolved.url)
        return resolved

    def _checkout(
        self, url: str, ref: str | None, dest: Path, sparse: SparseCheckout | None
    ) -> None:
        self._clone(
            url,
            ref,
            dest,
            token=self._policy.token_for(url),
            sparse=sparse,
            limits=self._policy.clone_limits,
        )

    def catalog(
        self,
        source: str,
        ref: str | None = None,
        *,
        path: str | None = None,
        refresh: bool = False,
    ) -> FetchedCatalog:
        """The repository's catalog under *path*, from cache unless stale or *refresh*."""
        resolved = self.resolve(source, ref, path)
        key = (resolved.url, resolved.ref, resolved.path)
        with self._lock:
            cached = self._catalogs.get(key)
        if (
            cached is not None
            and not refresh
            and time.monotonic() - cached.fetched_at < self._ttl_s
        ):
            return cached
        with tempfile.TemporaryDirectory(prefix="omnigent-skill-catalog-") as tmp:
            checkout = Path(tmp) / "repo"
            self._checkout(
                resolved.url,
                resolved.ref,
                checkout,
                SparseCheckout(catalog_patterns(resolved.path)),
            )
            skipped: list[str] = []
            entries = _index(checkout, resolved.path, skipped)
            fetched = FetchedCatalog(
                url=resolved.url,
                ref=resolved.ref,
                commit=checkout_commit(checkout),
                entries=tuple(entries),
                skipped=tuple(skipped),
                fetched_at=time.monotonic(),
                path=resolved.path,
            )
        self._remember(key, fetched)
        return fetched

    def package(
        self,
        source: str,
        ref: str | None,
        names: Sequence[str],
        *,
        path: str | None = None,
    ) -> tuple[str | None, list[PackagedSkill]]:
        """Clone once and package each named skill; a name the repo lacks raises.

        The clone first checks out the manifests under *path*, indexes them,
        then adds only the named skills' directories.

        :returns: The checkout's commit and the packaged skills in *names* order.
        :raises GitSourceError: On a clone failure, an unknown name, or an unsafe file.
        """
        resolved = self.resolve(source, ref, path)
        by_name: dict[str, CatalogEntry] = {}

        def expand(checkout: Path) -> list[str]:
            by_name.update((e.name, e) for e in _index(checkout, resolved.path, None))
            return [f"/{by_name[name].path}/" for name in names if name in by_name]

        with tempfile.TemporaryDirectory(prefix="omnigent-skill-install-") as tmp:
            checkout = Path(tmp) / "repo"
            self._checkout(
                resolved.url,
                resolved.ref,
                checkout,
                SparseCheckout(catalog_patterns(resolved.path), expand),
            )
            missing = [name for name in names if name not in by_name]
            if missing:
                where = f"{resolved.url} ({resolved.path})" if resolved.path else resolved.url
                raise GitSourceError(f"No such skill in {where}: {', '.join(sorted(missing))}")
            packaged = []
            total = 0
            for name in names:
                entry = by_name[name]
                tarball = tar_untrusted_dir(
                    checkout / entry.path,
                    what="a skill",
                    max_bytes=MAX_SKILL_BYTES,
                    max_entries=MAX_SKILL_ENTRIES,
                )
                total += len(tarball)
                if total > MAX_INSTALL_BYTES:
                    raise GitSourceError(
                        f"The requested skills exceed {MAX_INSTALL_BYTES // (1024 * 1024)} MB "
                        "together; install fewer at a time."
                    )
                packaged.append(PackagedSkill(entry=entry, tarball=tarball))
            return checkout_commit(checkout), packaged


def _index(checkout: Path, path: str, skipped: list[str] | None) -> list[CatalogEntry]:
    """The catalog under *path* of *checkout*, entry paths relative to the repository root."""
    root = checkout / path if path else checkout
    # A link anywhere along the way could point the index, and the packaging
    # after it, outside the checkout.
    if (
        root.is_symlink()
        or not root.is_dir()
        or not root.resolve().is_relative_to(checkout.resolve())
    ):
        if skipped is not None:
            skipped.append(f"{path}: not a directory in the repository")
        return []
    return index_checkout(root, skipped=skipped, prefix=path)
