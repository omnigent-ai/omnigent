"""Persistence for the skills marketplace: sources to browse and installed skills."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod

from omnigent.entities import InstalledSkill, SkillSource
from omnigent.entities.skill import SkillScope

# Every list read is bounded; the marketplace shows at most this many rows.
MAX_LIST = 200


class SkillStore(ABC):
    """Abstract store for marketplace sources and installed skills."""

    # ── sources ──────────────────────────────────────────────────────────

    @abstractmethod
    def add_source(
        self, *, url: str, ref: str | None, name: str, created_by: str | None
    ) -> SkillSource:
        """Add a repository to browse; the same ``(url, ref)`` twice returns the existing row."""

    @abstractmethod
    def list_sources(self, *, limit: int = MAX_LIST) -> builtins.list[SkillSource]:
        """Sources, oldest first, at most *limit*."""

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
        self, user_id: str | None, *, limit: int = MAX_LIST
    ) -> builtins.list[InstalledSkill]:
        """The org-scoped skills plus *user_id*'s own, oldest first, at most *limit*."""

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
