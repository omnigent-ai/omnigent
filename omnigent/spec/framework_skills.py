"""
Framework-owned skills that every Omnigent session gets.

The skill files ship inside the package under
``omnigent/onboarding/agent/skills/``. Native launches link them into the
agent bundle, and ``load_skill`` merges them into the agent's skill list,
so harnesses with and without native skill support see the same set.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Sequence
from pathlib import Path

import omnigent.onboarding
from omnigent.spec.types import SkillSpec

_log = logging.getLogger(__name__)

#: Skills added to every session, in listing order.
FRAMEWORK_SKILL_NAMES: tuple[str, ...] = ("build-omnigent", "omnigent-guide")


def framework_skills_dir() -> Path:
    """
    Return the directory that holds the framework skills.

    Anchored on the ``omnigent.onboarding`` package, so moving a caller
    cannot break the path.

    :returns: The skills directory, e.g.
        ``Path(".../site-packages/omnigent/onboarding/agent/skills")``.
    """
    return Path(omnigent.onboarding.__file__).resolve().parent / "agent" / "skills"


@functools.cache
def framework_skill_specs() -> tuple[SkillSpec, ...]:
    """
    Parse the framework skills once per process.

    A missing directory or skill means a broken install, so it is logged
    as a warning rather than skipped silently.

    :returns: The parsed skills in :data:`FRAMEWORK_SKILL_NAMES` order;
        empty when the package does not contain them.
    """
    from omnigent.spec.parser import _discover_skills

    skills_dir = framework_skills_dir()
    if not skills_dir.is_dir():
        _log.warning("Framework skills directory %s is missing", skills_dir)
        return ()
    by_name = {spec.name: spec for spec in _discover_skills(skills_dir, skipped=[])}
    missing = [name for name in FRAMEWORK_SKILL_NAMES if name not in by_name]
    if missing:
        _log.warning("Framework skills %s are missing from %s", missing, skills_dir)
    return tuple(by_name[name] for name in FRAMEWORK_SKILL_NAMES if name in by_name)


def with_framework_skills(skills: Sequence[SkillSpec]) -> list[SkillSpec]:
    """
    Append each framework skill that *skills* does not already name.

    A bundled skill with the same name wins, so an agent can replace a
    framework skill with its own version.

    :param skills: The agent's skills, e.g. ``agent_spec.skills``.
    :returns: A new list: *skills* followed by the missing framework skills.
    """
    present = {skill.name for skill in skills}
    return [*skills, *(spec for spec in framework_skill_specs() if spec.name not in present)]
