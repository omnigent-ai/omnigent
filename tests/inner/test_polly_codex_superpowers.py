from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POLLY_CODEX_BUNDLE = REPO_ROOT / "examples" / "polly" / "agents" / "codex"

SUPERPOWERS_SKILLS = {
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
}


def test_polly_codex_bundle_contains_superpowers_skills() -> None:
    """Polly's Codex agent bundle carries the vendored Superpowers skills."""
    from omnigent.spec.parser import parse

    spec = parse(POLLY_CODEX_BUNDLE)

    assert {skill.name for skill in spec.skills} >= SUPERPOWERS_SKILLS


def test_polly_codex_superpowers_surface_through_codex_discovery(tmp_path: Path) -> None:
    """Codex-native session discovery surfaces the vendored bundle skills."""
    from omnigent.spec.skill_sources import SkillSourceContext, resolve_harness_skills

    ctx = SkillSourceContext(
        roots=(POLLY_CODEX_BUNDLE,),
        home=tmp_path,
        skills_filter="all",
        bundle_dir=POLLY_CODEX_BUNDLE,
    )

    discovered = {skill.name for skill in resolve_harness_skills(ctx, "codex-native")}

    assert discovered >= SUPERPOWERS_SKILLS
    assert "using-superpowers" in discovered


def test_polly_codex_superpowers_populates_private_codex_home(tmp_path: Path) -> None:
    """
    The Codex-native launcher exposes bundle skills through CODEX_HOME/skills.
    """
    from omnigent.inner.codex_executor import populate_codex_skills_from_bundle

    codex_home = tmp_path / "codex-home"

    populate_codex_skills_from_bundle(codex_home, POLLY_CODEX_BUNDLE, "all")

    linked_skills = {path.name for path in (codex_home / "skills").iterdir()}
    assert linked_skills >= SUPERPOWERS_SKILLS
    assert (codex_home / "skills" / "using-superpowers" / "SKILL.md").is_file()
