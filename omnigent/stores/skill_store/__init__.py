"""Persistence for the skills marketplace: sources to browse and installed skills."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar

from omnigent.entities import InstalledSkill, SkillSource
from omnigent.entities.skill import SkillScope

# Every list read is a page of at most this many rows.
MAX_PAGE = 200
DEFAULT_PAGE = 50

# Stored-column bounds in characters (see SqlInstalledSkill / SqlSkillSource).
MAX_NAME_CHARS = 255
MAX_DISPLAY_NAME_CHARS = 256
MAX_DESCRIPTION_CHARS = 1024
MAX_URL_CHARS = 512
MAX_REF_CHARS = 255
MAX_PATH_CHARS = 1024
MAX_LOCATION_CHARS = 512

T = TypeVar("T")


class SkillValueError(ValueError):
    """A value would not fit its column; raised before anything is written."""


@dataclass(frozen=True)
class SkillPage(Generic[T]):
    """One page of a keyset-paginated list.

    :param data: The rows, in a stable order.
    :param has_more: Whether a page follows.
    :param last_id: Cursor for the next page (``after=``), or ``None``.
    """

    data: list[T]
    has_more: bool
    last_id: str | None


def check_bound(field: str, value: str | None, limit: int) -> None:
    """Raise :class:`SkillValueError` when *value* exceeds *limit* characters."""
    if value is not None and len(value) > limit:
        raise SkillValueError(f"{field} is longer than {limit} characters")


class SkillStore(ABC):
    """Abstract store for marketplace sources and installed skills."""

    # ── sources ──────────────────────────────────────────────────────────

    @abstractmethod
    def add_source(
        self, *, url: str, ref: str | None, name: str, created_by: str | None
    ) -> SkillSource:
        """Add a repository to browse; the same ``(url, ref)`` twice returns the existing row."""

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
        installed_by: str | None,
    ) -> InstalledSkill:
        """Record an installed skill; reinstalling a name in the same scope replaces it."""

    @abstractmethod
    def get(self, skill_id: str) -> InstalledSkill | None:
        """One installed skill by id."""

    @abstractmethod
    def list_for_user(
        self, user_id: str | None, *, limit: int = DEFAULT_PAGE, after: str | None = None
    ) -> SkillPage[InstalledSkill]:
        """A page of the org-scoped skills plus *user_id*'s own, oldest first."""

    @abstractmethod
    def update(
        self,
        skill_id: str,
        *,
        commit: str | None,
        bundle_location: str,
        description: str,
        display_name: str | None,
    ) -> InstalledSkill | None:
        """Replace the stored package after a refetch; ``None`` when absent."""

    @abstractmethod
    def set_enabled(self, skill_id: str, enabled: bool) -> InstalledSkill | None:
        """Toggle whether sessions receive the skill; ``None`` when absent."""

    @abstractmethod
    def delete(self, skill_id: str) -> InstalledSkill | None:
        """Remove an installed skill, returning the row so its tarball can be dropped."""
