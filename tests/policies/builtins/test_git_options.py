"""Policy classifiers account for Git's global options."""

import pytest

from omnigent.policies.builtins.github import _classify_git, github_policy
from omnigent.policies.builtins.working_dir import _classify_git as _classify_directory
from tests.policies.builtins.helpers import tool_call_event


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["-C", "/workspace"],
        ["-c", "color.ui=false"],
        ["--git-dir", "/workspace/.git"],
        ["--namespace", "example"],
        ["--config-env", "color.ui=COLOR_SETTING"],
        ["--no-pager"],
        ["--work-tree=/workspace"],
        ["--attr-source", "HEAD"],
        ["--attr-source=HEAD"],
        ["--exec-path=/opt/git/libexec"],
        ["-C/workspace"],
        ["-ccolor.ui=false"],
        ["-C", "/workspace", "-c", "color.ui=false", "--no-pager"],
    ],
)
@pytest.mark.parametrize("subcommand,kind", [("fetch", "read"), ("push", "write")])
def test_git_global_options_preserve_remote_operation(
    options: list[str], subcommand: str, kind: str
) -> None:
    operation = _classify_git(
        ["git", *options, subcommand, "https://github.com/example/repo", "main"]
    )
    assert operation is not None
    assert operation.kind == kind
    assert operation.repo == "example/repo"
    assert operation.detail == f"git {subcommand}"


@pytest.mark.parametrize(
    "arguments",
    [
        ["status"],
        ["--no-pager", "diff"],
        ["--version"],
        ["--literal-pathspecs", "status"],
        ["--no-optional-locks", "status"],
        ["--exec-path", "status"],
        ["--exec-path", "fetch"],
    ],
)
def test_git_local_operations_remain_local(arguments: list[str]) -> None:
    assert _classify_git(["git", *arguments]) is None


@pytest.mark.parametrize(
    "arguments",
    [
        ["--future-option", "value", "status"],
        ["--future-option=value", "status"],
        ["--attr-source"],
        ["-C"],
    ],
)
def test_unsupported_or_incomplete_git_options_request_approval(arguments: list[str]) -> None:
    event = tool_call_event("sys_os_shell", {"command": " ".join(["git", *arguments])})
    result = github_policy()(event)
    assert result is not None
    assert result["result"] == "ASK"


@pytest.mark.parametrize("option", [["--work-tree", "/workspace"], ["--work-tree=/workspace"]])
def test_git_work_tree_is_a_directory_operation(option: list[str]) -> None:
    operation = _classify_directory(["git", *option, "status"], True, True)
    assert operation is not None
    assert operation.kind == "cd"
    assert operation.target == "/workspace"
    assert _classify_directory(["git", *option, "status"], False, True) is None


@pytest.mark.parametrize("option", [["--attr-source", "HEAD"], ["--attr-source=HEAD"]])
def test_git_attribute_source_preserves_worktree_classification(option: list[str]) -> None:
    operation = _classify_directory(["git", *option, "worktree", "add", "/workspace"], True, True)
    assert operation is not None
    assert operation.kind == "worktree"
    assert operation.detail == "git worktree add"
