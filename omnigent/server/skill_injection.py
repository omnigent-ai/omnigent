"""Fold a session owner's installed skills into the agent bundle a runner fetches.

The runner discovers ``skills/<name>/SKILL.md`` inside its bundle for every
harness, so adding installed skills there needs no runner change. A failure
on one skill drops that skill and logs it; the bundle itself always ships.
"""

from __future__ import annotations

import logging

from omnigent.entities import Agent
from omnigent.runtime.agent_cache import AgentCache
from omnigent.skills.overlay import SkillTarballError, overlay_skills
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
) -> bytes:
    """Return the bundle with the owner's enabled installed skills added under ``skills/``."""
    try:
        installed = skill_store.list_for_user(owner_id)
    except Exception:
        _logger.warning("could not list installed skills for %s", owner_id, exc_info=True)
        return bundle_bytes
    for skill in installed:
        if not skill.enabled or not skill_allowed(spec, skill.name):
            continue
        try:
            tarball = artifact_store.get(skill.bundle_location)
            bundle_bytes = overlay_skills(bundle_bytes, [(skill.name, tarball)])
        except (SkillTarballError, OSError, KeyError, ValueError):
            _logger.warning(
                "skipping installed skill %s (%s): unreadable tarball",
                skill.name,
                skill.id,
                exc_info=True,
            )
    return bundle_bytes
