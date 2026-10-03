"""Tests for :mod:`omnigent.runner.git_providers.azure_devops_observer`.

The segments are built directly, as the observer hands them to a facet after it
splits a shell command and drops environment prefixes and wrappers.
"""

from __future__ import annotations

import shlex
from typing import Any

import pytest

from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.azure_devops import PULL_REQUESTS
from omnigent.runner.git_providers.azure_devops_observer import (
    mcp_prs,
    pr_from_object,
    shell_pr_operations,
)
from omnigent.runner.session_prs import PullRequestRef

ORG = "https://dev.azure.com/contoso"
WEB_URL = "https://dev.azure.com/contoso/web/_git/app"
API_URL = "https://dev.azure.com/contoso/5e1f/_apis/git/repositories/9a8b"
READ = ShellPrOp(tracks=False, creates=False, target=None, content_only=False)
CREATE = ShellPrOp(tracks=True, creates=True, target=None, content_only=False)


def segment(command: str) -> ShellSegment:
    """Return one simple command without environment prefixes or wrappers."""
    tokens = tuple(shlex.split(command))
    return ShellSegment(raw_tokens=tokens, invocation_tokens=tokens)


def op(command: str) -> ShellPrOp:
    """Return the only op that ``command`` gives."""
    (operation,) = shell_pr_operations([segment(command)])
    return operation


def reference(number: int = 7) -> PullRequestRef:
    return PullRequestRef(
        provider="azure_devops",
        host="dev.azure.com",
        repository="contoso/web/app",
        number=number,
        url=f"https://dev.azure.com/contoso/web/_git/app/pullrequest/{number}",
    )


def pull_request(**fields: Any) -> dict[str, Any]:
    """Return a pull request object as ``az repos pr show`` prints it."""
    return {
        "pullRequestId": 7,
        "title": "Add the pipeline",
        # A REST API URL with ids, which the generic ``url`` check does not read as a PR.
        "url": "https://dev.azure.com/contoso/5e1f/_apis/git/repositories/9a8b/pullRequests/7",
        "repository": {"id": "9a8b", "name": "app", "webUrl": WEB_URL},
        **fields,
    }


# ---------------------------------------------------------------------------
# Shell commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        "create --title Fix --source-branch feature",
        "update --id 7 --title Fix",
        "update --id 7 --status completed",
        "update --id 7 --status abandoned",
        "update --id 7 --status active",
        "update --id 7 --draft true",
        "update --id 7 --auto-complete true",
        "set-vote --id 7 --vote approve",
        "reviewer add --id 7 --reviewers pat@contoso.com",
        "reviewer remove --id 7 --reviewers pat@contoso.com",
        "work-item add --id 7 --work-items 42",
        "work-item remove --id 7 --work-items 42",
        "policy queue --id 7 --evaluation-id 5",
    ],
)
def test_writes_track_the_pr(arguments: str) -> None:
    assert op(f"az repos pr {arguments}") == ShellPrOp(
        tracks=True,
        creates=arguments.startswith("create "),
        target=None,
        content_only=False,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        "show --id 7",
        "list --status active",
        "checkout --id 7",
        "reviewer list --id 7",
        "work-item list --id 7",
        "policy list --id 7",
        "reviewer",
        "--help",
        "",
        "future-command --id 7",
    ],
)
def test_other_subcommands_read(arguments: str) -> None:
    assert op(f"az repos pr {arguments}") == READ


def test_create_names_no_target() -> None:
    assert op(f"az repos pr create --org {ORG} -p web -r app --title Fix") == CREATE


@pytest.mark.parametrize(
    "flags",
    [
        f"--id 7 --vote approve --org {ORG} -p web -r app",
        f"--id=7 --vote=approve --organization={ORG} --project=web --repository=app",
        f"--org {ORG}/ --project web --repository app --vote approve --id 7",
    ],
)
def test_a_full_flag_set_names_the_target(flags: str) -> None:
    assert op(f"az repos pr set-vote {flags}") == ShellPrOp(
        tracks=True, creates=False, target=reference(), content_only=False
    )


@pytest.mark.parametrize(
    "flags",
    [
        "--id 7 -p web -r app",
        f"--id 7 --org {ORG} -r app",
        f"--id 7 --org {ORG} -p web",
        f"--org {ORG} -p web -r app",
        f"--id 7 --org {ORG} -p web -r",
        f"--id seven --org {ORG} -p web -r app",
        f"--id 0 --org {ORG} -p web -r app",
        "--id 7 --org https://github.com/contoso -p web -r app",
        "--id 7 --org contoso -p web -r app",
    ],
)
def test_a_partial_or_invalid_flag_set_names_no_target(flags: str) -> None:
    operation = op(f"az repos pr set-vote --vote approve {flags}")

    assert operation.tracks
    assert operation.target is None


@pytest.mark.parametrize(
    "org",
    [
        "https://contoso.visualstudio.com",
        "https://contoso.visualstudio.com/",
        "https://contoso.visualstudio.com/DefaultCollection",
    ],
)
def test_a_visualstudio_org_url_canonicalizes(org: str) -> None:
    command = f"az repos pr update --id 7 --status completed --org {org} -p Web -r App"

    assert op(command).target == reference()


def test_target_names_are_percent_encoded() -> None:
    target = op(f"az repos pr update --id 7 --org {ORG} -p 'Web Site' -r app").target

    assert target is not None
    assert target.url == "https://dev.azure.com/contoso/web%20site/_git/app/pullrequest/7"


@pytest.mark.parametrize(
    "command",
    [
        "/opt/homebrew/bin/az repos pr create --title Fix",
        "./az repos pr create --title Fix",
        "az --only-show-errors repos pr create --title Fix",
        "az --debug --verbose repos pr create --title Fix",
        "az -o json repos pr create --title Fix",
        "az --output=json repos pr create --title Fix",
    ],
)
def test_az_counts_by_basename_and_after_global_flags(command: str) -> None:
    assert op(command) == CREATE


def test_environment_prefixes_are_not_the_command() -> None:
    raw = ("AZURE_DEVOPS_EXT_PAT=secret", "az", "repos", "pr", "create", "--title", "Fix")

    assert shell_pr_operations([ShellSegment(raw_tokens=raw, invocation_tokens=raw[1:])]) == [
        CREATE
    ]


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title Fix",
        "git push origin feature",
        "az account show",
        "az repos list",
        "az repos show -r app",
        "az boards work-item create --title Fix --type Bug",
        "az --query repos pr create",
        "echo az repos pr create",
    ],
)
def test_other_commands_give_no_op(command: str) -> None:
    assert shell_pr_operations([segment(command)]) == []


def test_one_op_per_recognized_segment_in_order() -> None:
    segments = [
        segment("git push origin feature"),
        segment("az repos pr create --title Fix"),
        segment("gh pr view"),
        segment("az repos pr show --id 7"),
    ]

    assert shell_pr_operations(segments) == [CREATE, READ]


@pytest.mark.parametrize(
    ("arguments", "content_only"),
    [
        ("show --id 7 --query description", True),
        ("show --id 7 --query title", True),
        ("show --id 7 --query=description", True),
        ("show --id 7 --query '[title, description]'", True),
        ("update --id 7 --description Done --query description", True),
        ("show --id 7", False),
        ("show --id 7 --query pullRequestId", False),
        ("show --id 7 --query '[title, url]'", False),
        ("show --id 7 --query '[]'", False),
        ("update --id 7 --status completed -o tsv", False),
    ],
)
def test_content_only_when_the_query_selects_only_description_and_title(
    arguments: str, content_only: bool
) -> None:
    assert op(f"az repos pr {arguments}").content_only is content_only


# ---------------------------------------------------------------------------
# Output objects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "web_url",
    [
        WEB_URL,
        "https://dev.azure.com/Contoso/Web/_git/App",
        "https://contoso.visualstudio.com/web/_git/app",
        "https://contoso.visualstudio.com/DefaultCollection/web/_git/app",
    ],
)
def test_pr_from_object_reads_the_id_and_the_repository_web_url(web_url: str) -> None:
    obj = pull_request(repository={"name": "app", "webUrl": web_url})

    assert pr_from_object(obj) == reference()


@pytest.mark.parametrize(
    "repository",
    [
        {"remoteUrl": "https://contoso@dev.azure.com/contoso/web/_git/app"},
        {
            "webUrl": None,
            "remoteUrl": "https://contoso.visualstudio.com/DefaultCollection/web/_git/app",
        },
        {"webUrl": "https://github.com/contoso/app", "remoteUrl": WEB_URL},
    ],
)
def test_pr_from_object_falls_back_to_the_remote_url(repository: dict[str, Any]) -> None:
    assert pr_from_object(pull_request(repository=repository)) == reference()


@pytest.mark.parametrize(
    "api_url",
    [
        API_URL,
        "https://dev.azure.com/contoso/_apis/git/repositories/9a8b",
        "https://contoso.visualstudio.com/_apis/git/repositories/9a8b",
        "https://contoso.visualstudio.com/DefaultCollection/_apis/git/repositories/9a8b",
    ],
)
def test_pr_from_object_builds_the_repository_from_names_under_the_api_url(api_url: str) -> None:
    rest = {"name": "app", "project": {"name": "web"}, "url": api_url}
    az = {**rest, "name": "App", "project": {"id": "5e1f", "name": "Web"}, "webUrl": None}

    assert pr_from_object(pull_request(repository=rest)) == reference()
    assert pr_from_object(pull_request(repository={**az, "remoteUrl": None})) == reference()


@pytest.mark.parametrize(
    "repository",
    [
        {
            "webUrl": WEB_URL,
            "remoteUrl": "https://dev.azure.com/contoso/web/_git/other",
            "name": "other",
            "project": {"name": "web"},
            "url": API_URL,
        },
        {"remoteUrl": WEB_URL, "name": "other", "project": {"name": "web"}, "url": API_URL},
    ],
)
def test_the_first_repository_match_wins(repository: dict[str, Any]) -> None:
    assert pr_from_object(pull_request(repository=repository)) == reference()


@pytest.mark.parametrize(
    "repository",
    [
        {"name": "app", "project": {"name": "web"}},
        {"project": {"name": "web"}, "url": API_URL},
        {"name": "app", "url": API_URL},
        {"name": "app", "project": {"id": "5e1f"}, "url": API_URL},
        {"name": "app", "project": "web", "url": API_URL},
        {"name": "", "project": {"name": "web"}, "url": API_URL},
        {"name": 7, "project": {"name": "web"}, "url": API_URL},
        {"name": "app", "project": {"name": "web"}, "url": 7},
        {"name": "app", "project": {"name": "web"}, "url": WEB_URL},
        {"name": "app", "project": {"name": "web"}, "url": "https://dev.azure.com/_apis/git"},
        {"name": "app", "project": {"name": "web"}, "url": "http://dev.azure.com/contoso/_apis"},
        {"name": "app", "project": {"name": "web"}, "url": "https://ghe.example/o/_apis/r"},
    ],
)
def test_pr_from_object_needs_all_three_names(repository: dict[str, Any]) -> None:
    assert pr_from_object(pull_request(repository=repository)) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"pullRequestId": None},
        {"pullRequestId": "7"},
        {"pullRequestId": True},
        {"pullRequestId": 7.0},
        {"pullRequestId": 0},
        {"repository": None},
        {"repository": WEB_URL},
        {"repository": {"name": "app"}},
        {"repository": {"webUrl": None}},
        {"repository": {"webUrl": 7}},
        {"repository": {"webUrl": "https://github.com/contoso/app"}},
        {"repository": {"webUrl": "https://dev.azure.com/contoso/web"}},
    ],
)
def test_pr_from_object_needs_an_integer_id_and_an_azure_devops_repository(
    fields: dict[str, Any],
) -> None:
    assert pr_from_object(pull_request(**fields)) is None


@pytest.mark.parametrize("key", ["pullRequestId", "repository"])
def test_pr_from_object_without_a_field(key: str) -> None:
    obj = pull_request()
    del obj[key]

    assert pr_from_object(obj) is None


def test_no_mcp_tool_is_recognized() -> None:
    assert mcp_prs("repo_create_pull_request", {"repositoryId": "app"}, pull_request()) is None


# ---------------------------------------------------------------------------
# Facet wiring
# ---------------------------------------------------------------------------


def test_the_facet_answers_with_the_observer_hooks() -> None:
    assert PULL_REQUESTS.shell_pr_operations([segment("az repos pr create")]) == [CREATE]
    assert PULL_REQUESTS.pr_from_object(pull_request()) == reference()
    assert PULL_REQUESTS.mcp_prs("repo_create_pull_request", {}, pull_request()) is None
