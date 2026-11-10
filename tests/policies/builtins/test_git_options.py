"""Policy classifiers account for Git's global options."""

import pytest

from omnigent.policies.builtins.github import (
    _classify_git,
    _parse_git_push_args,
    github_policy,
)
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


def _push_verdict(command: str, *, restrict_branches: bool = True) -> str | None:
    """Return the decision for a shell ``git push`` under an ``example/allowed`` allowlist."""
    policy = github_policy(
        write_repos=["example/allowed"],
        write_branches=["main"] if restrict_branches else None,
    )
    result = policy(tool_call_event("sys_os_shell", {"command": command}))
    return result["result"] if result else "ALLOW"


def test_push_option_values_are_not_remote_arguments() -> None:
    parsed = _parse_git_push_args(
        [
            "--push-option",
            "https://github.com/example/other",
            "https://github.com/example/allowed",
            "main",
        ]
    )
    assert parsed is not None
    assert parsed[0] == "https://github.com/example/allowed"
    assert parsed[1] == ["main"]


def test_attached_push_option_values_are_not_remote_arguments() -> None:
    parsed = _parse_git_push_args(
        ["-ohttps://github.com/example/other", "https://github.com/example/allowed", "main"]
    )
    assert parsed is not None
    assert parsed[0] == "https://github.com/example/allowed"


def test_push_policy_checks_the_actual_remote() -> None:
    assert (
        _push_verdict(
            "git push https://github.com/example/other main --push-option=https://github.com/example/allowed"
        )
        == "DENY"
    )


def test_push_to_unknown_remote_alias_requires_approval() -> None:
    assert _push_verdict("git push origin main") == "ASK"


def test_unrecognized_push_option_requires_approval() -> None:
    assert (
        _push_verdict("git push --unknown-option https://github.com/example/allowed main") == "ASK"
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push --unknown-option --force https://github.com/example/allowed main",
        "git push --unknown-option --delete https://github.com/example/allowed main",
        "git push --unknown-option --tags https://github.com/example/allowed",
        "git push --unknown-option https://github.com/example/allowed :main",
        "git push --unknown-option https://github.com/example/allowed +main",
    ],
)
def test_unrecognized_push_option_preserves_push_denials(command: str) -> None:
    assert _push_verdict(command) == "DENY"


def test_negated_recurse_submodules_push_option_is_supported() -> None:
    assert (
        _push_verdict("git push --no-recurse-submodules https://github.com/example/allowed main")
        == "ALLOW"
    )


@pytest.mark.parametrize("option", ["--all", "--branches"])
def test_whole_branch_push_requires_approval_when_branches_are_restricted(option: str) -> None:
    command = f"git push {option} https://github.com/example/allowed"
    assert _push_verdict(command) == "ASK"
    assert _push_verdict(command, restrict_branches=False) == "ALLOW"


def test_mirror_and_prune_pushes_are_denied_by_default() -> None:
    remote = "https://github.com/example/allowed"
    assert _push_verdict(f"git push --mirror {remote}") == "DENY"
    assert _push_verdict(f"git push --prune {remote}") == "DENY"


def test_repo_option_is_the_only_push_authority_when_present() -> None:
    assert _push_verdict("git push --repo=https://github.com/example/other") == "DENY"
    assert _push_verdict("git push --repo https://github.com/example/allowed") == "ASK"
    assert (
        _push_verdict(
            "git push --repo https://github.com/example/allowed", restrict_branches=False
        )
        == "ALLOW"
    )
    assert (
        _parse_git_push_args(
            [
                "--repo",
                "https://github.com/example/allowed",
                "https://github.com/example/other",
                "main",
            ]
        )
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push -uf https://github.com/example/allowed main",
        "git push --force-with-lease=main https://github.com/example/allowed main",
        "git push https://github.com/example/allowed +main",
    ],
)
def test_force_push_forms_remain_denied(command: str) -> None:
    assert _push_verdict(command) == "DENY"


@pytest.mark.parametrize(
    "command",
    [
        "git push --delete https://github.com/example/allowed main",
        "git push -d https://github.com/example/allowed main",
        "git push https://github.com/example/allowed :main",
    ],
)
def test_delete_push_forms_remain_denied(command: str) -> None:
    assert _push_verdict(command) == "DENY"


def test_allowlisted_push_remains_allowed() -> None:
    assert _push_verdict("git push https://github.com/example/allowed main") == "ALLOW"
