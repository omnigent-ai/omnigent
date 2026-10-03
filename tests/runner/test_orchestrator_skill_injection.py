"""Tests for framework skill injection into native bundles and ``load_skill``.

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
from omnigent.runner.native import orchestration
from omnigent.runner.native.orchestration import (
    _ensure_orchestrator_skills_in_bundle,
)
from omnigent.runner.tool_dispatch import _execute_skill_tool
from omnigent.spec import load

SKILL_NAME = "build-omnigent"
FRAMEWORK_SKILLS = ["build-omnigent", "slide-decks"]
_POLLY_BUNDLE = Path(__file__).resolve().parents[2] / "examples" / "polly"


def test_skill_source_lives_in_the_installed_package() -> None:
    """The canonical source directory ships inside the package."""
    source = Path(omnigent.__file__).resolve().parent / "onboarding" / "agent" / "skills"
    assert (source / SKILL_NAME / "SKILL.md").is_file()


def test_injects_skills_into_empty_bundle(tmp_path: Path) -> None:
    """A bare bundle gets every framework skill with a readable SKILL.md."""
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    for name in FRAMEWORK_SKILLS:
        target = tmp_path / "skills" / name
        assert (target / "SKILL.md").is_file(), f"{name} was not injected into the bundle"


def test_injection_is_idempotent(tmp_path: Path) -> None:
    """Re-running on the same bundle neither raises nor duplicates."""
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    assert sorted(p.name for p in (tmp_path / "skills").iterdir()) == FRAMEWORK_SKILLS


def test_bundle_skill_of_same_name_is_kept(tmp_path: Path) -> None:
    """A bundle's own ``skills/slide-decks`` is not replaced by the framework copy."""
    own = tmp_path / "skills" / "slide-decks"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: slide-decks\ndescription: Mine.\n---\n\nMine.\n")

    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    assert not own.is_symlink()
    assert "Mine." in (own / "SKILL.md").read_text()


def test_missing_source_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A framework skill whose source dir is gone is skipped, the rest still link."""
    real = orchestration.FRAMEWORK_SKILL_DIRS
    monkeypatch.setattr(
        orchestration, "FRAMEWORK_SKILL_DIRS", (tmp_path / "gone" / "missing", *real)
    )
    bundle = tmp_path / "bundle"

    _ensure_orchestrator_skills_in_bundle(bundle, None)

    assert sorted(p.name for p in (bundle / "skills").iterdir()) == FRAMEWORK_SKILLS


def test_polly_loads_slide_decks_without_bundling_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """polly (claude-sdk) reaches slide-decks through the runner's load_skill."""
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = load(_POLLY_BUNDLE)
    assert "slide-decks" not in {s.name for s in spec.skills}

    loaded = _execute_skill_tool(
        "load_skill", {"name": "slide-decks"}, agent_spec=spec, runner_workspace=tmp_path
    )

    assert "## Deck format" in loaded


def test_codex_resolves_the_injected_skill(tmp_path: Path) -> None:
    """The injected skill reaches Codex's real skill-source resolution.

    Guards the downstream half: ``codex_skill_sources`` only returns the
    bundle root when ``<bundle>/skills`` exists, so an injector that links
    nothing drops the skill even though both sides look correct alone.
    """
    _ensure_orchestrator_skills_in_bundle(tmp_path, None)

    sources = codex_skill_sources(tmp_path, tmp_path / "fake-home")
    assert sources == [tmp_path / "skills"]
    assert SKILL_NAME in select_codex_skill_dirs("all", sources)
