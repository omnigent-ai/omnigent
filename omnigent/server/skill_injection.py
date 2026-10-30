"""Fold a session owner's installed skills into the agent bundle a runner fetches.

The runner discovers ``skills/<name>/SKILL.md`` inside the directory of the
spec it runs (the bundle root, or ``agents/<dir>/`` for a child), so adding
installed skills under that directory needs no runner change. One bad
tarball drops only itself; exceeding the delivery bounds is an error the
caller sees rather than a skill silently missing from the menu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from omnigent.entities import Agent, InstalledSkill
from omnigent.runtime.agent_cache import AgentCache
from omnigent.skills.limits import (
    MAX_DELIVERY_BYTES,
    MAX_DELIVERY_ENTRIES,
    MAX_INSTALLED_PER_OWNER,
    MAX_PAGE,
    RUNNER_UNPACK_BYTES,
    RUNNER_UNPACK_ENTRIES,
)
from omnigent.skills.overlay import (
    SkillTarballError,
    UnpackedSize,
    overlay_skills,
    unpacked_size,
    validate_skill_tarball,
)
from omnigent.skills.policy import DEFAULT_POLICY, SkillsMarketplacePolicy
from omnigent.spec.types import AgentSpec
from omnigent.spec.validator import is_valid_skill_name
from omnigent.stores import ArtifactStore
from omnigent.stores.skill_store import SkillStore

_logger = logging.getLogger(__name__)

# A user's own rows plus the org's, each capped at install time.
MAX_DELIVERED_SKILLS = 2 * MAX_INSTALLED_PER_OWNER


class SkillDeliveryError(Exception):
    """The installed skills exceed what one session can be given; the message is user-facing."""


@dataclass(frozen=True)
class SessionTarget:
    """The spec a session runs and where its files live inside the bundle.

    :param spec: The parsed spec, or ``None`` when the bundle could not be loaded.
    :param prefix: ``""`` for the root agent, else ``agents/<dir>/`` per level of nesting.
    """

    spec: AgentSpec | None
    prefix: str


def bundle_spec(agent_cache: AgentCache | None, agent: Agent) -> AgentSpec | None:
    """The agent's parsed spec, for its ``skills:`` filter; ``None`` when unavailable."""
    if agent_cache is None:
        return None
    try:
        return agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        ).spec
    except Exception:  # noqa: BLE001 — the filter is advisory; the bundle still ships
        _logger.warning("could not load spec for agent %s to filter skills", agent.id)
        return None


def session_target(spec: AgentSpec | None, sub_agent_name: str | None) -> SessionTarget:
    """Resolve the child a session runs, by declared name at any depth, and its directory.

    An unknown name falls back to the root, as the runner does.
    """
    if spec is None or not sub_agent_name:
        return SessionTarget(spec, "")
    found = _find_sub_agent(spec, sub_agent_name, "")
    return found if found is not None else SessionTarget(spec, "")


def _find_sub_agent(spec: AgentSpec, name: str, prefix: str) -> SessionTarget | None:
    for child in spec.sub_agents:
        child_prefix = f"{prefix}agents/{child.source_rel_dir or child.name}/"
        if child.name == name:
            return SessionTarget(child, child_prefix)
        nested = _find_sub_agent(child, name, child_prefix)
        if nested is not None:
            return nested
    return None


def skill_allowed(spec: AgentSpec | None, name: str) -> bool:
    """Apply the agent spec's ``skills:`` filter the way host skills honour it."""
    skills_filter = getattr(spec, "skills_filter", "all") if spec is not None else "all"
    if skills_filter == "none":
        return False
    if isinstance(skills_filter, list):
        return name in skills_filter
    return True


def with_installed_skills(
    bundle_bytes: bytes,
    *,
    owner_id: str | None,
    skill_store: SkillStore,
    artifact_store: ArtifactStore,
    target: SessionTarget | None = None,
    policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
) -> bytes:
    """Return the bundle with the owner's enabled installed skills under the target's ``skills/``.

    The target spec's own skills, by their canonical parsed names, always win
    a collision; a user-scoped install wins over an org-scoped one of the same
    name. A tarball that fails validation drops only itself, and so does a
    skill whose repository the server's policy no longer allows.

    A name the runner's spec validator refuses is skipped, since one invalid
    skill would fail the whole spec.

    :param target: The spec the session runs and its directory in the bundle;
        ``None`` means the root agent with no filter.
    :raises SkillDeliveryError: When the skills exceed :data:`MAX_DELIVERED_SKILLS`,
        :data:`MAX_DELIVERY_BYTES` or :data:`MAX_DELIVERY_ENTRIES` together, or
        the merged bundle would exceed what a runner unpacks.
    """
    target = target or SessionTarget(None, "")
    installed = effective_installed_skills(skill_store, owner_id, policy=policy)
    reserved = {skill.name for skill in getattr(target.spec, "skills", None) or []}
    accepted: list[tuple[str, bytes]] = []
    total = UnpackedSize(0, 0)
    for skill in installed:
        if not skill_allowed(target.spec, skill.name) or skill.name in reserved:
            continue
        if not is_valid_skill_name(skill.name):
            _logger.warning("skipping installed skill %r (%s): invalid name", skill.name, skill.id)
            continue
        try:
            tarball = artifact_store.get(skill.bundle_location)
            size = validate_skill_tarball(tarball)
        except (SkillTarballError, OSError, KeyError, ValueError):
            _logger.warning(
                "skipping installed skill %s (%s): unreadable tarball",
                skill.name,
                skill.id,
                exc_info=True,
            )
            continue
        total += size
        if total.bytes > MAX_DELIVERY_BYTES or total.entries > MAX_DELIVERY_ENTRIES:
            raise SkillDeliveryError(
                f"Installed skills exceed {MAX_DELIVERY_BYTES // (1024 * 1024)} MB or "
                f"{MAX_DELIVERY_ENTRIES} files together; disable or remove some in "
                "Settings → Skills."
            )
        accepted.append((skill.name, tarball))
    if accepted:
        try:
            merged = unpacked_size(bundle_bytes) + total
        except SkillTarballError:
            # Not a readable archive: the overlay below falls back to serving it as is.
            merged = total
        if merged.bytes > RUNNER_UNPACK_BYTES or merged.entries > RUNNER_UNPACK_ENTRIES:
            raise SkillDeliveryError(
                "This agent's files and its installed skills together exceed what a "
                f"session can unpack ({RUNNER_UNPACK_BYTES // (1024 * 1024)} MB, "
                f"{RUNNER_UNPACK_ENTRIES} files); disable or remove some in Settings → Skills."
            )
    try:
        return overlay_skills(bundle_bytes, accepted, reserved=reserved, prefix=target.prefix)
    except SkillTarballError:
        _logger.warning(
            "could not overlay installed skills; serving the bundle as is", exc_info=True
        )
        return bundle_bytes


def effective_installed_skills(
    skill_store: SkillStore,
    owner_id: str | None,
    *,
    policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
) -> list[InstalledSkill]:
    """The owner's enabled installed skills, one per name, user scope winning over org.

    A skill from a repository outside the policy's ``allowed_sources`` is left
    out, so tightening the policy stops serving it. Reads at most
    :data:`MAX_DELIVERED_SKILLS` rows, two full pages.

    :raises SkillDeliveryError: When more rows than that are visible.
    """
    by_name: dict[str, InstalledSkill] = {}
    after: str | None = None
    seen = 0
    while True:
        page = skill_store.list_for_user(owner_id, limit=MAX_PAGE, after=after)
        for skill in page.data:
            if not skill.enabled or not policy.allows(skill.source_url):
                continue
            current = by_name.get(skill.name)
            if current is None or (current.scope == "org" and skill.scope == "user"):
                by_name[skill.name] = skill
        seen += len(page.data)
        if seen > MAX_DELIVERED_SKILLS:
            raise SkillDeliveryError(
                f"More than {MAX_DELIVERED_SKILLS} installed skills are visible to this "
                "session; remove some in Settings → Skills."
            )
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return list(by_name.values())
