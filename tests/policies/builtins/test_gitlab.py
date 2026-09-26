"""Focused host-aware GitLab access policy coverage."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.gitlab import gitlab_policy
from tests.policies.builtins.helpers import tool_call_event as tc

_HOST = "https://gitlab.example"
_PROJECT = "group/subgroup/project"


def _shell(command: str):
    return tc("sys_os_shell", {"command": command})


def _decision(result):
    return result["result"] if result else "ALLOW"


def test_restricted_reads_accept_nested_project_and_instance_url() -> None:
    policy = gitlab_policy(read_all=False, read_repos=[f"{_HOST}/{_PROJECT}"], gitlab_host=_HOST)
    assert _decision(policy(_shell(f"glab mr view -R {_PROJECT}"))) == "ALLOW"
    assert _decision(policy(_shell("glab mr view -R group/other"))) == "DENY"


@pytest.mark.parametrize(
    "command, expected",
    [
        (f"glab mr create -R {_PROJECT} --target-branch main", "ALLOW"),
        (f"glab mr create -R {_PROJECT} --target-branch release", "DENY"),
        ("glab mr create -R group/other --target-branch main", "DENY"),
        ("glab mr create --target-branch main", "ASK"),
        (f"GLAB_HOST=other.example glab mr create -R {_PROJECT}", "ALLOW"),
    ],
)
def test_glab_writes_are_project_and_branch_scoped(command: str, expected: str) -> None:
    policy = gitlab_policy(gitlab_host=_HOST, write_repos=[_PROJECT], write_branches=["main"])
    assert _decision(policy(_shell(command))) == expected


def test_git_remote_write_is_host_aware() -> None:
    policy = gitlab_policy(gitlab_host=_HOST, write_repos=[_PROJECT], write_branches=["main"])
    assert (
        _decision(policy(_shell(f"git push https://gitlab.example/{_PROJECT}.git main")))
        == "ALLOW"
    )
    assert (
        _decision(policy(_shell(f"git push https://other.example/{_PROJECT}.git main"))) == "ALLOW"
    )
    assert _decision(policy(_shell("git push origin main"))) == "ASK"


def test_gitlab_mcp_write_fails_closed_without_project() -> None:
    policy = gitlab_policy(gitlab_host=_HOST, write_repos=[_PROJECT])
    assert _decision(policy(tc("mcp__gitlab__create_merge_request", {}))) == "DENY"
    assert (
        _decision(policy(tc("mcp__gitlab__create_merge_request", {"project": _PROJECT})))
        == "ALLOW"
    )


def test_invalid_instance_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        gitlab_policy(gitlab_host="http://gitlab.example")
