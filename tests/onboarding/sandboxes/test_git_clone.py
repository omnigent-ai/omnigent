"""Git argument semantics and compatibility with custom workspace materializers."""

import shlex

import click
import pytest

from omnigent.onboarding.sandboxes.kubernetes import _render_workspace_prep_command
from omnigent.onboarding.sandboxes.types import GitCloneOptions, RepoWorkspace
from tests.onboarding.sandboxes.test_base import _RecordingLauncher


@pytest.mark.parametrize(
    ("options", "branch", "expected"),
    [
        (GitCloneOptions(), None, []),
        (GitCloneOptions(), "release", ["--branch", "release", "--single-branch"]),
        (GitCloneOptions(depth=50), None, ["--no-single-branch", "--depth", "50"]),
        (
            GitCloneOptions(depth=50, single_branch=True),
            None,
            ["--single-branch", "--depth", "50"],
        ),
        (
            GitCloneOptions(single_branch=False),
            "release",
            ["--branch", "release", "--no-single-branch"],
        ),
        (
            GitCloneOptions(filter="blob:none", tags=False),
            None,
            ["--filter=blob:none", "--no-tags"],
        ),
    ],
)
def test_clone_arguments(
    options: GitCloneOptions, branch: str | None, expected: list[str]
) -> None:
    assert options.clone_args(branch) == expected


def test_exec_and_kubernetes_quote_identical_clone_options() -> None:
    repo = RepoWorkspace(
        "https://example.test/org/repo.git",
        "release;literal",
        "repo",
        GitCloneOptions(50, False, "blob:none", False),
    )
    launcher = _RecordingLauncher()
    launcher.start_host(
        "sb",
        token="tok",
        host_id="host",
        host_name="host",
        server_url="https://example.test",
        repos=[repo],
    )
    clone = next(command for command in launcher.commands if command.startswith("git clone"))
    args = shlex.split(clone)
    assert args[2:-2] == [
        "--branch",
        "release;literal",
        "--no-single-branch",
        "--depth",
        "50",
        "--filter=blob:none",
        "--no-tags",
        "--",
    ]
    script = _render_workspace_prep_command(
        "/root/workspace", [repo], "https://example.test", "host"
    )[2]
    assert clone.replace("/root/workspace/repo", "/root/workspace/repo.tmp/clone") in script


def test_legacy_materializer_gets_no_new_keywords_and_rejects_opt_in() -> None:
    class LocalCheckout(_RecordingLauncher):
        def materialize_workspace(
            self, sandbox_id, *, workspace, repo_url, repo_branch, repo_name
        ):
            return "/existing/checkout"

    launcher = LocalCheckout()
    kwargs = {
        "token": "tok",
        "host_id": "host",
        "host_name": "host",
        "server_url": "https://example.test",
    }
    assert (
        launcher.start_host(
            "sb", repos=[RepoWorkspace("https://example.test/repo", None, "repo")], **kwargs
        )
        == "/existing/checkout"
    )
    assert not launcher.capabilities.git_clone_options
    launcher.commands.clear()
    with pytest.raises(click.ClickException, match=r"does not support sandbox\.git_clone"):
        launcher.start_host(
            "sb",
            repos=[
                RepoWorkspace("https://example.test/repo", None, "repo", GitCloneOptions(depth=50))
            ],
            **kwargs,
        )
    assert launcher.commands == []
