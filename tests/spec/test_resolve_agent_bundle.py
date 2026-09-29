import json
import re
from pathlib import Path

import pytest

from omnigent.cli import _bundle
from omnigent.inner.bundle_skills import claude_native_skill_args
from omnigent.runner.tool_dispatch import _execute_skill_tool
from omnigent.runtime.prompt import build_instructions
from omnigent.server.bundles import validate_agent_bundle
from omnigent.spec import load, materialize_bundle
from omnigent.tools.builtins.load_skill import LoadSkillTool, list_skill_resources

_AGENT = Path(__file__).resolve().parents[2] / "dev" / "resolve-agent"


@pytest.mark.parametrize("target_repo", ["omnigent", "omnigent-internal"])
def test_resolve_procedures_survive_transport_outside_target_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_repo: str
) -> None:
    workspace = tmp_path / target_repo
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "isolated-home")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    source = materialize_bundle(_AGENT, tmp_path / "source-agent")
    payload = _bundle(source)
    assert validate_agent_bundle(payload).name == "resolve_agent"
    bundle = tmp_path / "runner-agent"
    spec = load(payload, dest=bundle)
    assert not (workspace / "dev" / "resolve-agent").exists()

    for skill in spec.skills:
        assert skill.skill_dir is not None and skill.skill_dir.is_relative_to(bundle)
        loaded = _execute_skill_tool(
            "load_skill",
            {"name": skill.name},
            agent_spec=spec,
            runner_workspace=workspace,
        )
        assert skill.content in loaded
        for resource in list_skill_resources(skill):
            assert resource in loaded
            content = _execute_skill_tool(
                "read_skill_file",
                {"skill_name": skill.name, "path": resource},
                agent_spec=spec,
                runner_workspace=workspace,
            )
            assert content == (skill.skill_dir / resource).read_text()

    args = claude_native_skill_args(bundle, agent_name=spec.name, skills_filter=spec.skills_filter)
    assert args[:2] == ["--plugin-dir", str(bundle)]
    manifest = json.loads((bundle / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == spec.name


def test_resolve_startup_prompt_routes_to_procedures_without_inlining_them() -> None:
    spec = load(_AGENT)
    tool = LoadSkillTool(spec.skills, skills_filter="none")
    prompt = build_instructions(spec, None, [tool.get_schema()])

    assert len(prompt.split()) < 1500
    assert spec.skills
    for skill in spec.skills:
        assert skill.name in prompt
        assert skill.content not in prompt
    assert "reproduction-driven work, complete" in prompt
    assert "`resolve-repro-audit` before the existing-fix search" in prompt
    assert "Before every interim or final handoff" in prompt


def test_resolve_markdown_links_resolve_from_their_own_directory() -> None:
    # Moving procedures into nested skills must keep relative links pointing at real files.
    broken = []
    for document in sorted(_AGENT.rglob("*.md")):
        for target in re.findall(r"\]\(([^)\s]+)\)", document.read_text()):
            if re.match(r"[a-z][a-z0-9+.-]*:|#|/", target):
                continue
            if not (document.parent / target.split("#")[0]).exists():
                broken.append(f"{document.relative_to(_AGENT)} -> {target}")
    assert not broken
