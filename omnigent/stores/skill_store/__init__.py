"""Persistence for the skills marketplace: sources to browse and installed skills."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from omnigent.entities import InstalledSkill, SkillSource
from omnigent.entities.skill import SkillScope
from omnigent.skills.limits import (
    DEFAULT_PAGE,
    MAX_DELIVERY_BYTES_PER_SCOPE,
    MAX_DELIVERY_ENTRIES_PER_SCOPE,
    MAX_DESCRIPTION_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_INSTALLED_PER_OWNER,
    MAX_LOCATION_CHARS,
    MAX_NAME_CHARS,
    MAX_PAGE,
    MAX_PATH_CHARS,
    MAX_REF_CHARS,
    MAX_SOURCE_PATH_CHARS,
    MAX_URL_CHARS,
    MAX_USER_ID_CHARS,
)

__all__ = [
    "DEFAULT_PAGE",
    "MAX_DELIVERY_BYTES_PER_SCOPE",
    "MAX_DELIVERY_ENTRIES_PER_SCOPE",
    "MAX_DESCRIPTION_CHARS",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_INSTALLED_PER_OWNER",
    "MAX_LOCATION_CHARS",
    "MAX_NAME_CHARS",
    "MAX_PAGE",
    "MAX_PATH_CHARS",
    "MAX_REF_CHARS",
    "MAX_SOURCE_PATH_CHARS",
    "MAX_URL_CHARS",
    "MAX_USER_ID_CHARS",
    "InstallResult",
    "SkillInstall",
    "SkillPage",
    "SkillQuotaError",
    "SkillStore",
    "SkillValueError",
    "check_bound",
]

T = TypeVar("T")


class SkillValueError(ValueError):
    """A value would not fit its column; raised before anything is written."""


class SkillQuotaError(SkillValueError):
    """The write would take a principal past its row, byte or entry budget; nothing was written."""


@dataclass(frozen=True)
class SkillPage(Generic[T]):
    """One page of a keyset-paginated list.

    :param data: The rows, in a stable order.
    :param has_more: Whether a page follows.
    :param last_id: Opaque cursor for the next page (``after=``), or ``None``
        when the page is empty. It carries the last row's ordering values,
        so a row deleted between two pages cannot stall or truncate the walk.
    """

    data: list[T]
    has_more: bool
    last_id: str | None


@dataclass(frozen=True)
class SkillInstall:
    """One skill to record, as packaged from its repository."""

    name: str
    display_name: str | None
    description: str
    source_url: str
    ref: str | None
    commit: str | None
    path: str
    bundle_location: str
    # Unpacked bytes and entries of the tarball, counted against the budgets.
    bundle_bytes: int
    bundle_entries: int
    # From the skill's own frontmatter: false keeps it out of a user's menu
    # while it is still delivered to the agent.
    user_invocable: bool = True


@dataclass(frozen=True)
class InstallResult:
    """A recorded skill and, when it replaced a row, the tarball that row pointed at."""

    skill: InstalledSkill
    replaced_location: str | None


def check_bound(field: str, value: str | None, limit: int) -> None:
    """Raise :class:`SkillValueError` when *value* exceeds *limit* characters."""
    if value is not None and len(value) > limit:
        raise SkillValueError(f"{field} is longer than {limit} characters")


class SkillStore(ABC):
    """Abstract store for marketplace sources and installed skills.

    Every read is bounded: lists are keyset pages, lookups take a finite set
    of names, and counts stop at :data:`MAX_INSTALLED_PER_OWNER`. Writes that
    add to a principal's set are checked against its budgets and committed
    under one lock, so concurrent requests cannot overrun them.
    """

    # ── sources ──────────────────────────────────────────────────────────

    @abstractmethod
    def add_source(
        self, *, url: str, ref: str | None, name: str, created_by: str | None, path: str = ""
    ) -> SkillSource:
        """Add a repository (or a directory in one, *path*) to browse.

        The same ``(url, ref, path)`` twice returns the existing row.
        """

    @abstractmethod
    def find_source(self, url: str, ref: str | None, path: str = "") -> SkillSource | None:
        """The source for this exact ``(url, ref, path)``, if any."""

    @abstractmethod
    def list_sources(
        self, *, limit: int = DEFAULT_PAGE, after: str | None = None
    ) -> SkillPage[SkillSource]:
        """A page of sources, oldest first; ``after`` is the previous page's ``last_id``."""

    @abstractmethod
    def get_source(self, source_id: str) -> SkillSource | None:
        """One source by id."""

    @abstractmethod
    def delete_source(self, source_id: str) -> bool:
        """Remove a source; installed skills from it stay. ``False`` when absent."""

    # ── installed skills ─────────────────────────────────────────────────

    @abstractmethod
    def install_many(
        self,
        items: Sequence[SkillInstall],
        *,
        scope: SkillScope,
        owner_user_id: str | None,
        installed_by: str | None,
        max_rows: int | None = None,
        max_bytes: int | None = None,
        max_entries: int | None = None,
    ) -> list[InstallResult]:
        """Record *items* for one principal in one transaction: all or nothing.

        A name the principal already has is replaced in place, keeping its id.

        :raises SkillQuotaError: When the principal would exceed *max_rows*
            rows, or *max_bytes* or *max_entries* across its enabled skills
            unpacked (the module limits when ``None``); nothing is written.
        """

    def install(
        self,
        *,
        name: str,
        display_name: str | None,
        description: str,
        source_url: str,
        ref: str | None,
        commit: str | None,
        path: str,
        scope: SkillScope,
        owner_user_id: str | None,
        bundle_location: str,
        bundle_bytes: int,
        installed_by: str | None,
        user_invocable: bool = True,
        bundle_entries: int = 1,
    ) -> InstalledSkill:
        """Record one skill (see :meth:`install_many`)."""
        item = SkillInstall(
            name=name,
            display_name=display_name,
            description=description,
            source_url=source_url,
            ref=ref,
            commit=commit,
            path=path,
            bundle_location=bundle_location,
            bundle_bytes=bundle_bytes,
            bundle_entries=bundle_entries,
            user_invocable=user_invocable,
        )
        return self.install_many(
            [item], scope=scope, owner_user_id=owner_user_id, installed_by=installed_by
        )[0].skill

    @abstractmethod
    def get(self, skill_id: str) -> InstalledSkill | None:
        """One installed skill by id."""

    @abstractmethod
    def list_for_user(
        self, user_id: str | None, *, limit: int = DEFAULT_PAGE, after: str | None = None
    ) -> SkillPage[InstalledSkill]:
        """A page of the org-scoped skills plus *user_id*'s own, oldest first."""

    @abstractmethod
    def find_for_user(self, user_id: str | None, names: Collection[str]) -> list[InstalledSkill]:
        """The visible installs among *names* (org-scoped plus the user's own)."""

    @abstractmethod
    def count_installed(self, scope: SkillScope, owner_user_id: str | None) -> int:
        """Rows of one principal, counted no further than the per-principal cap."""

    @abstractmethod
    def update(
        self,
        skill_id: str,
        *,
        commit: str | None,
        bundle_location: str,
        bundle_bytes: int,
        bundle_entries: int,
        description: str,
        display_name: str | None,
        path: str | None = None,
        user_invocable: bool = True,
        max_bytes: int | None = None,
        max_entries: int | None = None,
    ) -> InstallResult | None:
        """Replace the stored package after a refetch; ``None`` when absent.

        The result names the tarball the row pointed at under the lock, so a
        caller drops what this write replaced and not what it read earlier.

        :raises SkillQuotaError: When the new size would exceed the principal's budgets.
        """

    @abstractmethod
    def set_enabled(
        self,
        skill_id: str,
        enabled: bool,
        *,
        max_bytes: int | None = None,
        max_entries: int | None = None,
    ) -> InstalledSkill | None:
        """Toggle whether sessions receive the skill; ``None`` when absent.

        :raises SkillQuotaError: When enabling would exceed the principal's budgets.
        """

    @abstractmethod
    def delete(self, skill_id: str) -> InstalledSkill | None:
        """Remove an installed skill, returning the row so its tarball can be dropped."""
