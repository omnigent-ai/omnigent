"""Fetch a repository's skill catalog and package the skills a user picks.

Clones are short-lived: the catalog is indexed and cached in memory per
``(url, ref)`` for :data:`CATALOG_TTL_S`, and an install clones again so the
packaged skill always matches the commit it reports.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from omnigent.skills.catalog import CatalogEntry, index_checkout, skill_markdown
from omnigent.skills.git_source import (
    GitSourceError,
    checkout_commit,
    clone_github_source,
    parse_github_source,
    tar_untrusted_dir,
)

CATALOG_TTL_S = 600.0
# Per skill and per install request; checked while packing, before reading more.
MAX_SKILL_BYTES = 20 * 1024 * 1024
MAX_SKILL_ENTRIES = 2_000
MAX_INSTALL_BYTES = 100 * 1024 * 1024

Cloner = Callable[[str, str | None, Path], None]


def server_clone(clone_url: str, ref: str | None, dest: Path) -> None:
    """The marketplace's clone: isolated from the server's own git credentials."""
    clone_github_source(clone_url, ref, dest, isolated=True)


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
    """Normalize ``owner/repo`` or a github.com URL into ``(clone_url, ref)``.

    :raises GitSourceError: When the source is not a GitHub repository.
    """
    parsed = parse_github_source(source.strip())
    if parsed is None:
        raise GitSourceError(
            "Only GitHub repositories are supported: use owner/repo or a github.com URL."
        )
    return parsed.clone_url, ref or parsed.ref


class SkillFetcher:
    """Clone, index and package skills from GitHub repositories."""

    def __init__(self, *, clone: Cloner = server_clone, ttl_s: float = CATALOG_TTL_S):
        self._clone = clone
        self._ttl_s = ttl_s
        self._catalogs: dict[tuple[str, str | None], FetchedCatalog] = {}
        self._lock = threading.Lock()

    def catalog(
        self, source: str, ref: str | None = None, *, refresh: bool = False
    ) -> FetchedCatalog:
        """The repository's catalog, from cache unless stale or *refresh*."""
        url, ref = resolve_source(source, ref)
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
            self._clone(url, ref, checkout)
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
        url, ref = resolve_source(source, ref)
        with tempfile.TemporaryDirectory(prefix="omnigent-skill-install-") as tmp:
            checkout = Path(tmp) / "repo"
            self._clone(url, ref, checkout)
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
