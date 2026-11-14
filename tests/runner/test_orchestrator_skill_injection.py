"""Tests for ``build-omnigent`` skill injection into native agent bundles.

The injector resolved its source path by counting ``.parent``s off its own
module file. Moving the module one package deeper left the count stale, so
the source directory did not exist and the ``not source.is_dir()`` guard
returned on every call — silently injecting nothing for every
``omnigent claude`` / ``omnigent codex`` user.

These tests pin the observable outcome (the skill lands in the bundle and
the Codex consumer resolves it) rather than the path expression, so the
next move of the module fails here instead of in the field.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent
from omnigent.inner.codex_executor import codex_skill_sources, select_codex_skill_dirs
from omnigent.runner.native.orchestration import (
    _ensure_orchestrator_skills_in_bundle,
)

SKILL_NAME = "build-omnigent"


def test_skill_source_lives_in_the_installed_package() -> None:
    """The canonical source directory ships inside the package."""
    source = Path(omnigent.__file__).resolve().parent / "onboarding" / "agent" / "skills"
    assert (source / SKILL_NAME / "SKILL.md").is_file()


def test_injects_skill_into_empty_bundle(tmp_path: Path) -> None:
    """A bare bundle gets a usable ``skills/build-omnigent`` with content."""
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    target = tmp_path / "skills" / SKILL_NAME
    assert target.is_dir(), f"{SKILL_NAME} was not injected into the bundle"
    assert (target / "SKILL.md").is_file(), "injected skill has no readable SKILL.md"


def test_injection_is_idempotent(tmp_path: Path) -> None:
    """Re-running on the same bundle neither raises nor duplicates."""
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    assert (tmp_path / "skills" / SKILL_NAME).is_dir()
    assert [p.name for p in (tmp_path / "skills").iterdir()] == [SKILL_NAME]


def test_codex_resolves_the_injected_skill(tmp_path: Path) -> None:
    """The injected skill reaches Codex's real skill-source resolution.

    Guards the downstream half: ``codex_skill_sources`` only returns the
    bundle root when ``<bundle>/skills`` exists, so an injector that links
    nothing drops the skill even though both sides look correct alone.
    """
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    sources = codex_skill_sources(tmp_path, tmp_path / "fake-home").linked
    assert sources == [tmp_path / "skills"]
    assert SKILL_NAME in select_codex_skill_dirs("all", sources)


@pytest.mark.parametrize("workflows_enabled", [False, True])
def test_native_workflow_skills_require_server_flag_and_journal_tools(
    tmp_path: Path, workflows_enabled: bool
) -> None:
    """Native workflow instructions must agree with the tools advertised by the server."""
    from omnigent.spec.types import AgentSpec, ToolGroupsConfig

    with_journal, without = tmp_path / "with", tmp_path / "without"
    spec = AgentSpec(spec_version=1)
    _ensure_orchestrator_skills_in_bundle(with_journal, spec, workflows_enabled=workflows_enabled)
    _ensure_orchestrator_skills_in_bundle(
        without,
        AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(journal=False)),
        workflows_enabled=workflows_enabled,
    )

    assert (with_journal / "skills" / "workflow" / "SKILL.md").is_file() is workflows_enabled
    assert (with_journal / "skills" / "save-workflow" / "SKILL.md").is_file() is workflows_enabled
    assert sorted(p.name for p in (with_journal / "skills").iterdir()) == sorted(
        [SKILL_NAME, *(["save-workflow", "workflow"] if workflows_enabled else [])]
    )
    assert [p.name for p in (without / "skills").iterdir()] == [SKILL_NAME]

    _ensure_orchestrator_skills_in_bundle(with_journal, spec, workflows_enabled=False)
    assert [p.name for p in (with_journal / "skills").iterdir()] == [SKILL_NAME]
    user_skill = with_journal / "skills" / "workflow" / "SKILL.md"
    user_skill.parent.mkdir()
    user_skill.write_text("User-authored workflow instructions")
    _ensure_orchestrator_skills_in_bundle(with_journal, spec, workflows_enabled=False)
    assert user_skill.read_text() == "User-authored workflow instructions"
