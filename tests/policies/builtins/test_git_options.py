"""Policy classifiers account for Git's global options."""

import pytest

from omnigent.policies.builtins.github import _classify_git
from omnigent.policies.builtins.working_dir import _classify_git as _classify_directory


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


@pytest.mark.parametrize("arguments", [["status"], ["--no-pager", "diff"], ["--version"]])
def test_git_local_operations_remain_local(arguments: list[str]) -> None:
    assert _classify_git(["git", *arguments]) is None


@pytest.mark.parametrize("option", [["--work-tree", "/workspace"], ["--work-tree=/workspace"]])
def test_git_work_tree_is_a_directory_operation(option: list[str]) -> None:
    operation = _classify_directory(["git", *option, "status"], True, True)
    assert operation is not None
    assert operation.kind == "cd"
    assert operation.target == "/workspace"
    assert _classify_directory(["git", *option, "status"], False, True) is None
