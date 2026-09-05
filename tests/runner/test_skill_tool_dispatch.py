"""Tests for the runner's load_skill / read_skill_file dispatch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.runner.tool_dispatch import _execute_skill_tool

SKILL_MD = (
    "---\nname: example\ndescription: An example skill.\n---\n\nBody of the example skill.\n"
)


def _write_host_skill(workspace: Path) -> None:
    """Install a host-scope skill with a root-level auxiliary file."""
    skill_dir = workspace / ".claude" / "skills" / "example"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    (skill_dir / "EXTRA.md").write_text("Auxiliary content.\n")


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin HOME so host-skill discovery cannot read the developer's own skills."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_read_skill_file_resolves_host_scope_skill(
    tmp_path: Path,
    isolated_home: Path,
) -> None:
    """A skill load_skill can load, read_skill_file can read."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_host_skill(workspace)
    spec = SimpleNamespace(skills=[], skills_filter="all")

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "example"},
        agent_spec=spec,
        runner_workspace=workspace,
    )
    assert "Body of the example skill." in loaded

    read = _execute_skill_tool(
        "read_skill_file",
        {"skill_name": "example", "path": "EXTRA.md"},
        agent_spec=spec,
        runner_workspace=workspace,
    )
    assert read == "Auxiliary content.\n"


def test_read_skill_file_resolves_bundled_skill(
    tmp_path: Path,
    isolated_home: Path,
) -> None:
    """Bundled skills keep working — the fix must not regress them."""
    from omnigent.spec.types import SkillSpec

    skill_dir = tmp_path / "bundle" / "skills" / "bundled"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    (skill_dir / "NOTES.md").write_text("Bundled notes.\n")
    spec = SimpleNamespace(
        skills=[
            SkillSpec(
                name="bundled",
                description="A bundled skill.",
                content="Bundled body.",
                skill_dir=skill_dir,
            )
        ],
        skills_filter="all",
    )

    read = _execute_skill_tool(
        "read_skill_file",
        {"skill_name": "bundled", "path": "NOTES.md"},
        agent_spec=spec,
        runner_workspace=tmp_path / "bundle",
    )
    assert read == "Bundled notes.\n"


def test_skill_tools_agree_when_filter_suppresses_host_skills(
    tmp_path: Path,
    isolated_home: Path,
) -> None:
    """skills_filter='none' hides the skill from BOTH tools, not just one."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_host_skill(workspace)
    spec = SimpleNamespace(skills=[], skills_filter="none")

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "example"},
        agent_spec=spec,
        runner_workspace=workspace,
    )
    read = _execute_skill_tool(
        "read_skill_file",
        {"skill_name": "example", "path": "EXTRA.md"},
        agent_spec=spec,
        runner_workspace=workspace,
    )
    assert "not found" in loaded
    assert "not found" in read


def test_load_then_read_auxiliary_file_reported_sequence(
    tmp_path: Path,
    isolated_home: Path,
) -> None:
    """load_skill advertises the auxiliary file, and read_skill_file returns it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".claude" / "skills" / "codebase-design"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: codebase-design\ndescription: Designs codebases.\n---\n\nDesign body.\n"
    )
    (skill_dir / "DEEPENING.md").write_text("Deepening content.\n")
    (skill_dir / "DESIGN-IT-TWICE.md").write_text("Design it twice content.\n")
    spec = SimpleNamespace(skills=[], skills_filter="all")

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "codebase-design"},
        agent_spec=spec,
        runner_workspace=workspace,
    )
    assert "## Available files" in loaded
    assert "- DEEPENING.md" in loaded
    assert "- DESIGN-IT-TWICE.md" in loaded

    for path, expected in (
        ("DEEPENING.md", "Deepening content.\n"),
        ("DESIGN-IT-TWICE.md", "Design it twice content.\n"),
    ):
        assert (
            _execute_skill_tool(
                "read_skill_file",
                {"skill_name": "codebase-design", "path": path},
                agent_spec=spec,
                runner_workspace=workspace,
            )
            == expected
        )
