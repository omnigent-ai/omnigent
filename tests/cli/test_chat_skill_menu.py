"""The REPL's slash-command menu offers what the web composer offers.

``_merge_host_skills`` feeds the REPL's skill commands; the host builds the web
menu for the same session with ``resolve_session_skills``. These pin the two
together across harness tiers and a generated agent root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent import chat
from omnigent.chat import _merge_host_skills
from omnigent.spec.skill_sources import resolve_session_skills
from omnigent.spec.types import AgentSpec, ExecutorSpec, SkillSpec


def _write_skill(skills_dir: Path, name: str, description: str | None = None) -> None:
    (skills_dir / name).mkdir(parents=True)
    (skills_dir / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name}\n---\nbody\n"
    )


def _enable_plugin(claude_dir: Path, plugin: str, skill: str) -> None:
    """Install and enable one Claude Code plugin that ships *skill*."""
    _write_skill(claude_dir / "plugins" / "cache" / plugin / "skills", skill)
    (claude_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {f"{plugin}@local": True}})
    )
    (claude_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    f"{plugin}@local": [{"scope": "user", "installPath": f"cache/{plugin}"}]
                },
            }
        )
    )


def _spec(harness: str, *, bundled: list[SkillSpec] | None = None) -> AgentSpec:
    return AgentSpec(spec_version=1, executor=ExecutorSpec(type=harness), skills=bundled or [])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    return workspace


def test_claude_repl_menu_lists_plugin_and_project_skills_like_the_web_menu(
    home: Path, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(home / ".claude" / "skills", "user-tier-skill")
    _enable_plugin(home / ".claude", "figplug", "plug-skill")
    _write_skill(workspace / ".claude" / "skills", "ws-proj-skill")
    _write_skill(workspace / ".claude" / "skills", "shared", "workspace copy")
    # ``omnigent run --harness`` materializes the spec outside the project.
    generated_root = tmp_path / "omnigent-override"
    _write_skill(generated_root / ".claude" / "skills", "shared", "agent-root copy")
    materialized = generated_root / "agent.yaml"
    monkeypatch.setitem(chat._MATERIALIZED_OVERRIDE_DIRS, materialized.resolve(), generated_root)
    spec = _spec("claude-sdk")

    merged = _merge_host_skills(spec, materialized)

    repl = {s.name for s in merged}
    assert repl == {"user-tier-skill", "figplug:plug-skill", "ws-proj-skill", "shared"}
    # The workspace is searched ahead of the agent root, so its copy wins.
    assert [s.description for s in merged if s.name == "shared"] == ["workspace copy"]
    web = resolve_session_skills(spec, (workspace, generated_root), generated_root)
    assert repl == {s.name for s in web}


def test_native_claude_repl_menu_honours_claude_config_dir(
    home: Path, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(home / ".claude" / "skills", "home-tier")
    config_dir = tmp_path / "claude-config"
    _write_skill(config_dir / "skills", "configdir-tier")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    names = {s.name for s in _merge_host_skills(_spec("claude-native"), workspace)}

    assert names == {"configdir-tier"}


def test_native_claude_keeps_the_workspace_ancestor_walk_for_an_unmaterialized_spec(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `omnigent run agent.yaml` from the agent's own directory with no overrides
    # keeps the original spec, so that directory is a workspace, not a bundle.
    mono = tmp_path / "mono"
    _write_skill(mono / ".agents" / "skills", "anc")
    project = mono / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    names = {s.name for s in _merge_host_skills(_spec("claude-native"), project / "agent.yaml")}

    assert "anc" in names


def test_codex_repl_menu_omits_skills_codex_never_registers(home: Path, workspace: Path) -> None:
    _write_skill(home / ".claude" / "skills", "claude-only")
    _write_skill(home / ".codex" / "skills", "codex-only")

    names = {s.name for s in _merge_host_skills(_spec("codex-native"), workspace)}

    assert "codex-only" in names
    assert "claude-only" not in names


def test_bundled_skill_still_wins_a_name_collision(home: Path, workspace: Path) -> None:
    _write_skill(workspace / ".claude" / "skills", "code-review")
    bundled = SkillSpec(name="code-review", description="bundled", content="body")

    merged = _merge_host_skills(_spec("claude-sdk", bundled=[bundled]), workspace)

    assert [s.description for s in merged if s.name == "code-review"] == ["bundled"]
