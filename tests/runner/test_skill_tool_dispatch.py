"""Tests for the runner's load_skill / read_skill_file dispatch."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner.tool_dispatch import _execute_skill_tool, execute_tool
from omnigent.spec.types import AgentSpec, ToolGroupsConfig

SKILL_MD = (
    "---\nname: example\ndescription: An example skill.\n---\n\nBody of the example skill.\n"
)


def _write_host_skill(workspace: Path, name: str = "example") -> None:
    """Install a host-scope skill with a root-level auxiliary file."""
    skill_dir = workspace / ".claude" / "skills" / name
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


@pytest.mark.parametrize(
    ("features", "journal"),
    [
        ({"workflows": True}, True),
        ({"workflows": False}, True),
        ({}, True),
        ({"workflows": True}, False),
    ],
)
async def test_framework_skills_load_only_when_server_and_tool_group_enable_them(
    tmp_path: Path,
    isolated_home: Path,
    features: dict[str, bool],
    journal: bool,
) -> None:
    spec = AgentSpec(
        spec_version=1,
        skills_filter="none",
        tool_groups=ToolGroupsConfig(journal=journal),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/info"
        return httpx.Response(200, json={"features": features})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server.test"
    ) as client:
        for name, heading in (
            ("workflow", "# Run a workflow"),
            ("save-workflow", "# Author a reusable workflow"),
        ):
            result = await execute_tool(
                tool_name="load_skill",
                arguments=json.dumps({"name": name}),
                agent_spec=spec,
                runner_workspace=tmp_path,
                server_client=client,
            )
            if features.get("workflows") and journal:
                assert heading in result
            else:
                assert "not found" in result


@pytest.mark.parametrize("name", ["example", "workflow"])
def test_read_skill_file_resolves_host_scope_skill(
    tmp_path: Path,
    isolated_home: Path,
    name: str,
) -> None:
    """A skill load_skill can load, read_skill_file can read."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_host_skill(workspace, name)
    spec = AgentSpec(spec_version=1, skills_filter="all")

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": name},
        agent_spec=spec,
        runner_workspace=workspace,
        workflows_enabled=True,
    )
    assert "Body of the example skill." in loaded

    read = _execute_skill_tool(
        "read_skill_file",
        {"skill_name": name, "path": "EXTRA.md"},
        agent_spec=spec,
        runner_workspace=workspace,
        workflows_enabled=True,
    )
    assert read == "Auxiliary content.\n"


@pytest.mark.parametrize("name", ["bundled", "save-workflow"])
def test_read_skill_file_resolves_bundled_skill(
    tmp_path: Path,
    isolated_home: Path,
    name: str,
) -> None:
    """Bundled skills keep working — the fix must not regress them."""
    from omnigent.spec.types import SkillSpec

    skill_dir = tmp_path / "bundle" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    (skill_dir / "NOTES.md").write_text("Bundled notes.\n")
    spec = AgentSpec(
        spec_version=1,
        skills=[
            SkillSpec(
                name=name,
                description="A bundled skill.",
                content="Bundled body.",
                skill_dir=skill_dir,
                user_invocable=False,
            )
        ],
        skills_filter="all",
    )

    assert _execute_skill_tool(
        "load_skill",
        {"name": name},
        agent_spec=spec,
        runner_workspace=tmp_path / "bundle",
        workflows_enabled=True,
    ).startswith("Bundled body.")
    read = _execute_skill_tool(
        "read_skill_file",
        {"skill_name": name, "path": "NOTES.md"},
        agent_spec=spec,
        runner_workspace=tmp_path / "bundle",
        workflows_enabled=True,
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
