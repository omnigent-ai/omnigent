"""Tests for omnigent.tools.builtins.load_skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.spec.types import SkillSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import LoadSkillTool


@pytest.fixture()
def skill_with_resources(tmp_path: Path) -> SkillSpec:
    """
    A skill with a ``references/`` directory containing a
    file, for testing resource listing in load_skill output.

    :returns: A ``SkillSpec`` pointing at a real directory
        with a reference file.
    """
    skill_dir = tmp_path / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "style-guide.md").write_text("# Style Guide\n\nUse snake_case.")
    return SkillSpec(
        name="code-review",
        description="Reviews code.",
        content="Review the code.",
        skill_dir=skill_dir,
    )


@pytest.fixture()
def skill_no_resources() -> SkillSpec:
    """
    A skill with no ``skill_dir`` (in-memory only).

    :returns: A ``SkillSpec`` with ``skill_dir=None``.
    """
    return SkillSpec(
        name="summarize",
        description="Summarizes text.",
        content="Summarize the input concisely.",
    )


def test_load_skill_returns_content(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns the skill's content string.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({"name": "summarize"}), tool_ctx)
    assert result == "Summarize the input concisely."


def test_load_skill_not_found(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns error for unknown skill name.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({"name": "nonexistent"}), tool_ctx)
    assert "not found" in result
    assert "summarize" in result


def test_load_skill_with_resources_lists_files(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke appends a resource listing when the
    skill has bundled reference files.
    """
    tool = LoadSkillTool([skill_with_resources])
    result = tool.invoke(
        json.dumps({"name": "code-review"}),
        tool_ctx,
    )
    assert "Review the code." in result
    assert "references/style-guide.md" in result
    assert "read_skill_file" in result


def test_load_skill_missing_name_argument(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns error when 'name' is missing.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({}), tool_ctx)
    assert "missing required 'name'" in result


def test_load_skill_schema_lists_skill_names(
    skill_no_resources: SkillSpec,
    skill_with_resources: SkillSpec,
) -> None:
    """
    LoadSkillTool.get_schema includes all skill names in the
    description.
    """
    tool = LoadSkillTool(
        [skill_no_resources, skill_with_resources],
    )
    schema = tool.get_schema()
    desc = schema["function"]["description"]
    assert "summarize" in desc
    assert "code-review" in desc
