"""Shell policies must not abstain when a command exceeds their parsing budget."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.github import github_policy
from omnigent.policies.builtins.working_dir import block_working_dir_changes
from tests.policies.builtins.helpers import tool_call_event


@pytest.mark.parametrize("depth,expected", [(4, "DENY"), (5, "ASK"), (8, "ASK")])
def test_github_nested_eval_requires_a_decision(depth: int, expected: str) -> None:
    command = "eval " * depth + "git push https://github.com/example/restricted main"
    result = github_policy()(tool_call_event("sys_os_shell", {"command": command}))
    assert result is not None
    assert result["result"] == expected


@pytest.mark.parametrize("depth", [4, 5, 8])
@pytest.mark.parametrize("action,expected", [("deny", "DENY"), ("ask", "ASK")])
def test_working_dir_nested_eval_requires_a_decision(
    depth: int, action: str, expected: str
) -> None:
    command = "eval " * depth + "cd /outside"
    policy = block_working_dir_changes(action=action)
    result = policy(tool_call_event("sys_os_shell", {"command": command}))
    assert result is not None
    assert result["result"] == expected
