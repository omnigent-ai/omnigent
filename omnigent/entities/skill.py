"""Skill marketplace entities: configured sources and installed skills."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SkillScope = Literal["user", "org"]


@dataclass(frozen=True)
class SkillSource:
    """A git repository the marketplace browses.

    :param id: Bare 32-char hex uuid.
    :param url: Clone URL, e.g. ``"https://github.com/acme/skills.git"``.
    :param ref: Branch or tag to fetch, or ``None`` for the default branch.
    :param name: Label shown in the UI, e.g. ``"Acme skills"``.
    :param created_by: User who added it, or ``None`` in single-user mode.
    :param created_at: Unix epoch seconds.
    """

    id: str
    url: str
    ref: str | None
    name: str
    created_by: str | None
    created_at: int


@dataclass(frozen=True)
class InstalledSkill:
    """A skill fetched from a repository and stored server-side.

    :param id: Bare 32-char hex uuid.
    :param name: Invocation identifier, the skill's directory name.
    :param display_name: Human-facing label from the frontmatter, or ``None``.
    :param description: One-line summary from the frontmatter.
    :param source_url: Clone URL it came from.
    :param ref: Branch or tag it was fetched at, or ``None``.
    :param commit: Commit it was packaged from, or ``None`` when unknown.
    :param path: Skill directory inside the repository, POSIX form.
    :param scope: ``"user"`` for one user's install, ``"org"`` for everyone.
    :param owner_user_id: The installing user for a user-scoped row, else ``None``.
    :param bundle_location: Artifact-store key of the skill tarball.
    :param bundle_bytes: Size of that tarball, counted against the owner's delivery budget.
    :param enabled: Whether sessions receive it.
    :param user_invocable: Whether its own ``SKILL.md`` offers it to the user;
        an internal orchestration skill is delivered but never listed in a menu.
    :param installed_by: User who installed it, or ``None`` in single-user mode.
    :param installed_at: Unix epoch seconds.
    :param updated_at: Unix epoch seconds of the last update, or ``None``.
    """

    id: str
    name: str
    display_name: str | None
    description: str
    source_url: str
    ref: str | None
    commit: str | None
    path: str
    scope: SkillScope
    owner_user_id: str | None
    bundle_location: str
    bundle_bytes: int
    enabled: bool
    user_invocable: bool
    installed_by: str | None
    installed_at: int
    updated_at: int | None
