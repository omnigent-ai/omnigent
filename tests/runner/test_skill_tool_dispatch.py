"""Regression tests for runner-local skill-tool dispatch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.runner.tool_dispatch import _execute_skill_tool


def test_host_skill_resource_read_uses_the_loader_catalog_and_keeps_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host skill loaded by the runner can read its advertised resource."""
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".agents" / "skills" / "host-research"
    references = skill_dir / "references"
    references.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: host-research\n"
        "description: Read local research guidance.\n"
        "---\n\n"
        "Use the local resource.\n"
    )
    (references / "guide.md").write_text("# Host guidance\n\nRead this first.\n")
    agent_spec = SimpleNamespace(skills=[], skills_filter="all")

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "host-research"},
        agent_spec=agent_spec,
        runner_workspace=workspace,
    )
    resource = _execute_skill_tool(
        "read_skill_file",
        {"skill_name": "host-research", "path": "references/guide.md"},
        agent_spec=agent_spec,
        runner_workspace=workspace,
    )
    outside = tmp_path / "outside.md"
    outside.write_text("must stay outside the skill root\n")
    (references / "escape.md").symlink_to(outside)
    escaped = _execute_skill_tool(
        "read_skill_file",
        {"skill_name": "host-research", "path": "references/escape.md"},
        agent_spec=agent_spec,
        runner_workspace=workspace,
    )

    assert "references/guide.md" in loaded
    assert "# Host guidance" in resource
    assert "path traversal not allowed" in escaped
