"""Fold a session owner's installed skills into the agent bundle a runner fetches.

The runner discovers ``skills/<name>/SKILL.md`` inside its bundle for every
harness, so adding installed skills there needs no runner change. A failure
on one skill drops that skill and logs it; the bundle itself always ships.
"""

from __future__ import annotations

import logging

from omnigent.entities import Agent, InstalledSkill
from omnigent.runtime.agent_cache import AgentCache
from omnigent.skills.overlay import SkillTarballError, overlay_skills, validate_skill_tarball
from omnigent.skills.policy import DEFAULT_POLICY, SkillsMarketplacePolicy
from omnigent.spec.types import AgentSpec
from omnigent.stores import ArtifactStore
from omnigent.stores.skill_store import SkillStore

_logger = logging.getLogger(__name__)


def _bundle_spec(agent_cache: AgentCache | None, agent: Agent) -> AgentSpec | None:
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
    spec: AgentSpec | None = None,
    policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
) -> bytes:
    """Return the bundle with the owner's enabled installed skills added under ``skills/``.

    The bundle's own skills, by their canonical parsed names, always win a
    collision; a user-scoped install wins over an org-scoped one of the same
    name. A tarball that fails validation drops only itself, and so does a
    skill whose repository the server's policy no longer allows.
    """
    try:
        installed = effective_installed_skills(skill_store, owner_id, policy=policy)
    except Exception:
        _logger.warning("could not list installed skills for %s", owner_id, exc_info=True)
        return bundle_bytes
    reserved = {skill.name for skill in getattr(spec, "skills", None) or []}
    accepted: list[tuple[str, bytes]] = []
    for skill in installed:
        if not skill_allowed(spec, skill.name) or skill.name in reserved:
            continue
        try:
            tarball = artifact_store.get(skill.bundle_location)
            validate_skill_tarball(tarball)
        except (SkillTarballError, OSError, KeyError, ValueError):
            _logger.warning(
                "skipping installed skill %s (%s): unreadable tarball",
                skill.name,
                skill.id,
                exc_info=True,
            )
            continue
        accepted.append((skill.name, tarball))
    try:
        return overlay_skills(bundle_bytes, accepted, reserved=reserved)
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
    out, so tightening the policy stops serving it. Pages through the store so
    the read stays bounded per call.
    """
    by_name: dict[str, InstalledSkill] = {}
    after: str | None = None
    while True:
        page = skill_store.list_for_user(owner_id, after=after)
        for skill in page.data:
            if not skill.enabled or not policy.allows(skill.source_url):
                continue
            current = by_name.get(skill.name)
            if current is None or (current.scope == "org" and skill.scope == "user"):
                by_name[skill.name] = skill
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return list(by_name.values())
