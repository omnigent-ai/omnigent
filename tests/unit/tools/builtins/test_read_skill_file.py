"""Tests for omnigent.tools.builtins.read_skill_file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.spec.types import SkillSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import ReadSkillFileTool


@pytest.fixture()
def skill_with_resources(tmp_path: Path) -> SkillSpec:
    """
    A skill with a ``references/`` directory containing a
    file, for testing ``read_skill_file``.

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


def test_read_skill_file_returns_content(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke reads a file from the skill dir.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "references/style-guide.md",
            }
        ),
        tool_ctx,
    )
    assert "# Style Guide" in result
    assert "snake_case" in result


def test_read_skill_file_unknown_skill(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error for unknown skill.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "nonexistent",
                "path": "references/style-guide.md",
            }
        ),
        tool_ctx,
    )
    assert "not found" in result
    assert "code-review" in result


def test_read_skill_file_traversal_blocked(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke rejects path traversal attempts.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "../../etc/passwd",
            }
        ),
        tool_ctx,
    )
    assert "traversal not allowed" in result


def test_read_skill_file_absolute_path_blocked(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke rejects absolute paths.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "/etc/passwd",
            }
        ),
        tool_ctx,
    )
    assert "path must be relative" in result


def test_read_skill_file_not_found(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error for missing files.
    """
    tool = ReadSkillFileTool([skill_with_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "code-review",
                "path": "references/nonexistent.md",
            }
        ),
        tool_ctx,
    )
    assert "file not found" in result


def test_read_skill_file_no_skill_dir(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error when skill has no
    directory on disk.
    """
    tool = ReadSkillFileTool([skill_no_resources])
    result = tool.invoke(
        json.dumps(
            {
                "skill_name": "summarize",
                "path": "references/foo.md",
            }
        ),
        tool_ctx,
    )
    assert "no directory on disk" in result


def test_read_skill_file_missing_arguments(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ReadSkillFileTool.invoke returns error when required
    arguments are missing.
    """
    tool = ReadSkillFileTool([skill_with_resources])

    result_no_name = tool.invoke(
        json.dumps({"path": "references/style-guide.md"}),
        tool_ctx,
    )
    assert "missing required 'skill_name'" in result_no_name

    result_no_path = tool.invoke(
        json.dumps({"skill_name": "code-review"}),
        tool_ctx,
    )
    assert "missing required 'path'" in result_no_path
