"""End-to-end skill loading for the shipped ``examples/superpowers`` bundle."""

from __future__ import annotations

from pathlib import Path

from omnigent.runner.tool_dispatch import _execute_skill_tool
from omnigent.spec import load

_SUPERPOWERS_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "superpowers"


def test_superpowers_loads_skill_and_cross_skill_resource() -> None:
    """The real bundle loads a skill and resolves its sibling-skill reference."""
    spec = load(_SUPERPOWERS_BUNDLE, expand_env=False)
    assert spec.name == "superpowers"
    assert len(spec.skills) == 15

    loaded = _execute_skill_tool(
        "load_skill",
        {"name": "subagent-driven-development"},
        agent_spec=spec,
        runner_workspace=_SUPERPOWERS_BUNDLE,
    )
    assert "## The Process" in loaded
    assert "task-reviewer-prompt.md" in loaded

    cross_skill_resource = _execute_skill_tool(
        "read_skill_file",
        {
            "skill_name": "subagent-driven-development",
            "path": "../requesting-code-review/code-reviewer.md",
        },
        agent_spec=spec,
        runner_workspace=_SUPERPOWERS_BUNDLE,
    )
    assert "# Code Reviewer" in cross_skill_resource
    assert "## What to Check" in cross_skill_resource
