"""Ownership classification (omnigent / agent / local) for skill candidates."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.spec.skill_registry import (
    SkillRegistry,
    is_platform_skill_path,
)
from omnigent.spec.skill_sources import registry_for_spec, resolve_all_candidates
from omnigent.spec.types import SkillSpec

# The real on-disk platform skills dir (where the universal build-omnigent lives)
# — used to prove path-based platform detection, not name matching.
_PLATFORM_DIR = Path(is_platform_skill_path.__globals__["_PLATFORM_SKILLS_DIR"])


def _spec(name: str, skills: list[SkillSpec]):
    """A minimal AgentSpec-like object for registry_for_spec()."""
    return SimpleNamespace(
        name=name,
        skills=skills,
        skills_filter="all",
        skill_trust="all-host",
        executor=SimpleNamespace(harness_kind="claude-sdk"),
    )


def _entry_by_name(registry: SkillRegistry, name: str):
    return next(e for e in registry.reconcile() if e.winner.invocation_name == name)


def test_bundled_skill_is_agent_owned_with_agent_name(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    sdir = bundle / "skills" / "cross-review"
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text("---\nname: cross-review\ndescription: d\n---\nbody\n")
    skill = SkillSpec(name="cross-review", description="d", content="body", skill_dir=sdir)

    reg = registry_for_spec(
        _spec("polly", [skill]),
        roots=(tmp_path / "empty-workspace",),
        home=tmp_path / "empty-home",
        bundle_dir=bundle,
        harness="claude-sdk",
    )
    winner = _entry_by_name(reg, "cross-review").winner
    assert winner.ownership == "agent"
    assert winner.agent_name == "polly"


def test_universal_build_omnigent_is_platform_owned_not_agent(tmp_path: Path) -> None:
    # The universal skill is injected into the bundle by symlinking the real
    # package dir; simulate that so its resolved path is the platform dir.
    platform_skill = _PLATFORM_DIR / "build-omnigent"
    if not platform_skill.is_dir():
        pytest.skip("platform build-omnigent dir not present in this checkout")
    bundle = tmp_path / "bundle"
    (bundle / "skills").mkdir(parents=True)
    link = bundle / "skills" / "build-omnigent"
    link.symlink_to(platform_skill)
    skill = SkillSpec(name="build-omnigent", description="d", content="body", skill_dir=link)

    reg = registry_for_spec(
        _spec("polly", [skill]),
        roots=(tmp_path / "empty-workspace",),
        home=tmp_path / "empty-home",
        bundle_dir=bundle,
        harness="claude-sdk",
    )
    winner = _entry_by_name(reg, "build-omnigent").winner
    # Platform-owned despite riding in polly's bundle — classified by PATH, not
    # by its name and not as an ordinary agent skill.
    assert winner.ownership == "omnigent"
    assert winner.agent_name is None


def test_host_discovered_skill_is_local(tmp_path: Path) -> None:
    # A skill under a fake ~/.claude/skills is host-discovered → local.
    home = tmp_path / "home"
    sdir = home / ".claude" / "skills" / "data-story"
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text("---\nname: data-story\ndescription: d\n---\nbody\n")

    reg = registry_for_spec(
        _spec("polly", []),
        roots=(tmp_path / "empty-workspace",),
        home=home,
        bundle_dir=None,
        harness="claude-sdk",
    )
    winner = _entry_by_name(reg, "data-story").winner
    assert winner.ownership == "local"
    assert winner.agent_name is None


def test_host_discovered_candidates_are_local_and_platform_marker_gates_omnigent(
    tmp_path: Path,
) -> None:
    # resolve_all_candidates walks .claude/.agents dirs under roots + home; those
    # are ordinary host skills → local (never the platform dir, which is only
    # reached via bundle injection). This proves the classifier defaults host
    # discovery to local, and that the platform marker is what flips omnigent.
    from omnigent.spec.skill_sources import SkillSourceContext

    ws = tmp_path / "ws"
    sdir = ws / ".claude" / "skills" / "local-one"
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text("---\nname: local-one\ndescription: d\n---\nbody\n")

    ctx = SkillSourceContext(
        roots=(ws,),
        home=tmp_path / "empty-home",
        skills_filter="all",
        bundle_dir=None,
    )
    cands = resolve_all_candidates(ctx)
    assert cands, "expected to discover the workspace .claude skill"
    assert all(c.ownership == "local" for c in cands)
    # The real platform build-omnigent dir is correctly recognized by the marker
    # (this is what the bundle-injection path relies on to flip it to omnigent).
    if (_PLATFORM_DIR / "build-omnigent").is_dir():
        assert is_platform_skill_path(_PLATFORM_DIR / "build-omnigent")


def test_duplicate_name_winner_keeps_its_own_ownership(tmp_path: Path) -> None:
    # A bundled (agent) skill and a host (local) skill share a name: the bundle
    # wins by precedence and the winner carries the AGENT ownership, not local.
    bundle = tmp_path / "bundle"
    bdir = bundle / "skills" / "shared"
    bdir.mkdir(parents=True)
    (bdir / "SKILL.md").write_text("---\nname: shared\ndescription: bundle\n---\nb\n")
    bundle_skill = SkillSpec(name="shared", description="bundle", content="b", skill_dir=bdir)

    home = tmp_path / "home"
    hdir = home / ".claude" / "skills" / "shared"
    hdir.mkdir(parents=True)
    (hdir / "SKILL.md").write_text("---\nname: shared\ndescription: host\n---\nh\n")

    reg = registry_for_spec(
        _spec("polly", [bundle_skill]),
        roots=(tmp_path / "empty-workspace",),
        home=home,
        bundle_dir=bundle,
        harness="claude-sdk",
    )
    entry = _entry_by_name(reg, "shared")
    assert entry.winner.ownership == "agent"
    assert entry.winner.agent_name == "polly"
    # The shadowed host candidate is retained and stays local.
    assert entry.shadowed
    assert all(c.ownership == "local" for c in entry.shadowed)
