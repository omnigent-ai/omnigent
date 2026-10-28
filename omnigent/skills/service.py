"""Fetch a repository's skill catalog and package the skills a user picks.

Clones are short-lived: the catalog is indexed and cached in memory per
``(url, ref)`` for :data:`CATALOG_TTL_S`, and an install clones again so the
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

from omnigent.skills.catalog import CatalogEntry, index_checkout, skill_markdown
from omnigent.skills.git_source import (
    GitSourceError,
    checkout_commit,
    clone_github_source,
    parse_github_source,
    tar_untrusted_dir,
)
from omnigent.skills.policy import DEFAULT_POLICY, SkillsMarketplacePolicy

CATALOG_TTL_S = 600.0
# Per skill and per install request; checked while packing, before reading more.
MAX_SKILL_BYTES = 20 * 1024 * 1024
MAX_SKILL_ENTRIES = 2_000
MAX_INSTALL_BYTES = 100 * 1024 * 1024


class Cloner(Protocol):
    """Clone *clone_url* at *ref* into *dest*; *token* is the operator's, if the policy has one."""

    def __call__(
        self, clone_url: str, ref: str | None, dest: Path, *, token: str | None = None
    ) -> None: ...


def server_clone(clone_url: str, ref: str | None, dest: Path, *, token: str | None = None) -> None:
    """The marketplace's clone: isolated from the server's own git credentials."""
    clone_github_source(clone_url, ref, dest, isolated=True, token=token)


@dataclass(frozen=True)
class FetchedCatalog:
    """A repository's skills as of one fetch."""

    url: str
    ref: str | None
    commit: str | None
    entries: tuple[CatalogEntry, ...]
    skipped: tuple[str, ...]
    fetched_at: float


@dataclass(frozen=True)
class PackagedSkill:
    """One skill ready to store: its catalog entry, its tarball, and its ``SKILL.md``."""

    entry: CatalogEntry
    tarball: bytes
    markdown: str


def resolve_source(source: str, ref: str | None = None) -> tuple[str, str | None]:
    """Normalize ``owner/repo`` or a GitHub URL into ``(clone_url, ref)``.

    :raises GitSourceError: When the source is not a GitHub repository.
    """
    parsed = parse_github_source(source.strip())
    if parsed is None:
        raise GitSourceError(
            "Only GitHub repositories are supported: use owner/repo, a github.com URL, "
            "or a URL on a GitHub Enterprise host this server knows."
        )
    return parsed.clone_url, ref or parsed.ref


class SkillFetcher:
    """Clone, index and package skills from GitHub repositories, within the server's policy."""

    def __init__(
        self,
        *,
        clone: Cloner = server_clone,
        ttl_s: float = CATALOG_TTL_S,
        policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
    ):
        self._clone = clone
        self._ttl_s = ttl_s
        self._policy = policy
        self._catalogs: dict[tuple[str, str | None], FetchedCatalog] = {}
        self._lock = threading.Lock()

    def resolve(self, source: str, ref: str | None = None) -> tuple[str, str | None]:
        """``(clone_url, ref)`` for a source the policy allows.

        :raises GitSourceError: When the source is not GitHub or the policy refuses it.
        """
        url, ref = resolve_source(source, ref)
        self._policy.require_allowed(url)
        return url, ref

    def _checkout(self, url: str, ref: str | None, dest: Path) -> None:
        self._clone(url, ref, dest, token=self._policy.token_for(url))

    def catalog(
        self, source: str, ref: str | None = None, *, refresh: bool = False
    ) -> FetchedCatalog:
        """The repository's catalog, from cache unless stale or *refresh*."""
        url, ref = self.resolve(source, ref)
        key = (url, ref)
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
            self._checkout(url, ref, checkout)
            skipped: list[str] = []
            entries = index_checkout(checkout, skipped=skipped)
            fetched = FetchedCatalog(
                url=url,
                ref=ref,
                commit=checkout_commit(checkout),
                entries=tuple(entries),
                skipped=tuple(skipped),
                fetched_at=time.monotonic(),
            )
        with self._lock:
            self._catalogs[key] = fetched
        return fetched

    def package(
        self, source: str, ref: str | None, names: Sequence[str]
    ) -> tuple[str | None, list[PackagedSkill]]:
        """Clone once and package each named skill; a name the repo lacks raises.

        :returns: The checkout's commit and the packaged skills in *names* order.
        :raises GitSourceError: On a clone failure, an unknown name, or an unsafe file.
        """
        url, ref = self.resolve(source, ref)
        with tempfile.TemporaryDirectory(prefix="omnigent-skill-install-") as tmp:
            checkout = Path(tmp) / "repo"
            self._checkout(url, ref, checkout)
            by_name = {entry.name: entry for entry in index_checkout(checkout)}
            missing = [name for name in names if name not in by_name]
            if missing:
                raise GitSourceError(f"No such skill in {url}: {', '.join(sorted(missing))}")
            packaged = []
            total = 0
            for name in names:
                entry = by_name[name]
                skill_dir = checkout / entry.path
                tarball = tar_untrusted_dir(
                    skill_dir,
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
                packaged.append(
                    PackagedSkill(entry=entry, tarball=tarball, markdown=skill_markdown(skill_dir))
                )
            return checkout_commit(checkout), packaged
