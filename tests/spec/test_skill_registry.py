from pathlib import Path

from omnigent.spec.parser import parse
from omnigent.spec.skill_registry import SkillCandidate, SkillRegistry, tree_digest
from omnigent.spec.types import AgentSpec, ExecutorSpec, SkillSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.manager import ToolManager


def _candidate(
    tmp_path: Path,
    name: str,
    provider: str,
    scope: str,
    content: str,
) -> SkillCandidate:
    skill_dir = tmp_path / provider / scope / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    skill = SkillSpec(name=name, description=provider, content=content, skill_dir=skill_dir)
    return SkillCandidate(
        provider=provider,
        location_scope=scope,  # type: ignore[arg-type]
        source_kind="bundled" if scope == "bundle" else "vendor",
        origin_path=skill_dir,
        source_coords=f"{provider}:{scope}:{name}",
        namespace=provider,
        invocation_name=name,
        managed=scope == "bundle",
        tree_digest=tree_digest(skill_dir),
        skill=skill,
    )


def test_precedence_trust_and_shadowing(tmp_path: Path) -> None:
    bundle = _candidate(tmp_path, "shared", "bundle", "bundle", "bundle")
    workspace = _candidate(tmp_path, "shared", "codex", "workspace", "workspace")
    personal = _candidate(tmp_path, "shared", "claude", "personal", "personal")

    current = SkillRegistry.from_candidates(
        [personal, workspace, bundle], active_provider="claude", skill_trust="current"
    ).list()[0]
    assert current.winner is bundle
    assert [item.provider for item in current.shadowed] == ["claude"]

    all_host = SkillRegistry.from_candidates(
        [personal, workspace, bundle], active_provider="claude", skill_trust="all-host"
    ).list()[0]
    assert all_host.winner is bundle
    assert [item.location_scope for item in all_host.shadowed] == ["workspace", "personal"]


def test_identity_stable_while_tree_digest_changes(tmp_path: Path) -> None:
    first = _candidate(tmp_path, "stable", "claude", "workspace", "one")
    first_entry = SkillRegistry([first]).list()[0]
    (first.origin_path / "references.md").write_text("two")
    second = SkillCandidate(**{**first.__dict__, "tree_digest": tree_digest(first.origin_path)})
    second_entry = SkillRegistry([second]).list()[0]
    assert first_entry.canonical_id == second_entry.canonical_id
    assert first.tree_digest != second.tree_digest


def test_tool_manager_load_skill_uses_registry(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "cross", "codex", "personal", "cross-provider")
    registry = SkillRegistry.from_candidates(
        [candidate], active_provider="claude", skill_trust="all-host"
    )
    spec = AgentSpec(spec_version=1, executor=ExecutorSpec())
    manager = ToolManager(spec, workdir=tmp_path, skill_registry=registry)
    result = manager.call_tool(
        "load_skill",
        '{"name":"cross"}',
        ToolContext(task_id="t", conversation_id="c", agent_id="a"),
    )
    assert "cross-provider" in result


def test_skill_trust_parser_default_and_opt_in(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("spec_version: 1\n")
    assert parse(tmp_path).skill_trust == "current"
    (tmp_path / "config.yaml").write_text("spec_version: 1\nskill_trust: all-host\n")
    assert parse(tmp_path).skill_trust == "all-host"
