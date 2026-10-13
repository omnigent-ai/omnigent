"""Tests for framework skill injection into native bundles and the relay.

The injector once resolved its source path by counting ``.parent``s off its
own module file. Moving the module one package deeper left the count stale,
so the source directory did not exist and the ``not source.is_dir()`` guard
returned on every call — silently injecting nothing for every
``omnigent claude`` / ``omnigent codex`` user.

These tests pin the observable outcome (the skills land in the bundle, the
Codex consumer resolves them, and ``load_skill`` loads them) rather than the
path expression, so the next move of the module fails here instead of in the
field.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent
from omnigent.inner.codex_executor import codex_skill_sources, select_codex_skill_dirs
from omnigent.runner.native.orchestration import (
    _ensure_orchestrator_skills_in_bundle,
)
from omnigent.runner.tool_dispatch import _execute_skill_tool
from omnigent.spec.framework_skills import FRAMEWORK_SKILL_NAMES, framework_skill_specs


@pytest.mark.parametrize("skill_name", FRAMEWORK_SKILL_NAMES)
def test_skill_source_lives_in_the_package(skill_name: str) -> None:
    """Each framework skill sits under the package's onboarding skills dir.

    Shipping it in the wheel is pinned separately by ``tests/test_package_data.py``.
    """
    source = Path(omnigent.__file__).resolve().parent / "onboarding" / "agent" / "skills"
    assert (source / skill_name / "SKILL.md").is_file()


def test_injects_skills_into_empty_bundle(tmp_path: Path) -> None:
    """A bare bundle gets every framework skill with a readable SKILL.md."""
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    for skill_name in FRAMEWORK_SKILL_NAMES:
        target = tmp_path / "skills" / skill_name
        assert target.is_dir(), f"{skill_name} was not injected into the bundle"
        assert (target / "SKILL.md").is_file(), f"{skill_name} has no readable SKILL.md"


def test_injection_is_idempotent(tmp_path: Path) -> None:
    """Re-running on the same bundle neither raises nor duplicates."""
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    names = sorted(p.name for p in (tmp_path / "skills").iterdir())
    assert names == sorted(FRAMEWORK_SKILL_NAMES)


def test_bundle_copy_of_a_framework_skill_is_kept(tmp_path: Path) -> None:
    """A skill the bundle already ships is not replaced by the framework copy."""
    own = tmp_path / "skills" / "omnigent-guide"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: omnigent-guide\ndescription: mine\n---\nmine\n")

    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    assert not own.is_symlink()
    assert "mine" in (own / "SKILL.md").read_text()


def test_codex_resolves_the_injected_skills(tmp_path: Path) -> None:
    """The injected skills reach Codex's real skill-source resolution.

    Guards the downstream half: ``codex_skill_sources`` only returns the
    bundle root when ``<bundle>/skills`` exists, so an injector that links
    nothing drops the skills even though both sides look correct alone.
    """
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    sources = codex_skill_sources(tmp_path, tmp_path / "fake-home")
    assert sources == [tmp_path / "skills"]
    selected = select_codex_skill_dirs("all", sources)
    for skill_name in FRAMEWORK_SKILL_NAMES:
        assert skill_name in selected


@pytest.mark.parametrize("skill_name", FRAMEWORK_SKILL_NAMES)
def test_relay_load_skill_loads_framework_skill_without_a_spec(
    skill_name: str, tmp_path: Path
) -> None:
    """A native relay ``load_skill`` call finds the skill with no bundled skills."""
    expected = next(spec.content for spec in framework_skill_specs() if spec.name == skill_name)

    output = _execute_skill_tool(
        "load_skill", {"name": skill_name}, agent_spec=None, runner_workspace=tmp_path
    )

    assert output.startswith(expected), output[:300]
