"""Push status identifies the updated remote branch without inspecting the checkout."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.runner import github_resource, pr_observer
from omnigent.runner.git_providers import github_push
from omnigent.runner.git_providers.github_push import push_operation, pushed_prs
from omnigent.runner.pr_observer import _shell_segments, observe_tool_completion
from omnigent.runner.session_prs import SessionPrRegistry

PR_URL = "https://github.com/upstream/project/pull/42"
PUSH_OUTPUT = (
    "To github.com:contributor/project.git\n   abc1234..def5678  local -> feature/topic\n"
)


@pytest.mark.parametrize(
    "command,output",
    [
        ("git push origin local:feature/topic", PUSH_OUTPUT),
        ("cd /workspaces/repo2 && git push && gh pr view", PUSH_OUTPUT),
        ("git -C /workspaces/repo2 push origin HEAD:feature/topic", PUSH_OUTPUT),
        ("git push -uf --push-option ci.skip origin local:feature/topic", PUSH_OUTPUT),
        (
            "git push --force-with-lease origin HEAD:feature/topic",
            "To https://github.com/contributor/project.git\n"
            " + abc1234...def5678 HEAD -> feature/topic (forced update)\n",
        ),
        (
            "git push -u origin feature/topic",
            "To git@github.com:contributor/project.git\n"
            " * [new branch] feature/topic -> feature/topic\n",
        ),
        (
            "git push --porcelain origin local:feature/topic",
            "To ssh://git@github.com/contributor/project.git\n"
            " \trefs/heads/local:refs/heads/feature/topic\tabc1234..def5678\nDone\n",
        ),
    ],
)
def test_push_tracks_only_its_open_head_including_forks(
    monkeypatch: pytest.MonkeyPatch, command: str, output: str
) -> None:
    matching = {
        "state": "open",
        "head": {"ref": "feature/topic", "repo": {"full_name": "Contributor/Project"}},
        "html_url": PR_URL,
    }
    gh = Mock(
        return_value=(
            0,
            json.dumps(
                [
                    {**matching, "state": "closed"},
                    {
                        **matching,
                        "head": {"ref": "main", "repo": {"full_name": "contributor/project"}},
                    },
                    {
                        **matching,
                        "head": {"ref": "feature/topic", "repo": {"full_name": "other/project"}},
                    },
                    matching,
                ]
            ),
            "",
        )
    )
    monkeypatch.setattr(github_resource, "_gh", gh)
    segments = _shell_segments(command)
    assert push_operation(segments) is not None
    assert [
        reference.url for reference in pushed_prs(segments, {"stderr": output, "exit_code": 0})
    ] == [PR_URL]
    assert gh.call_args.args[0] == [
        "api",
        "--hostname",
        "github.com",
        "repos/contributor/project/commits/feature%2Ftopic/pulls",
    ]
    assert gh.call_args.kwargs["timeout"] <= 5


@pytest.mark.parametrize(
    "command,result",
    [
        ("gh pr view", {"stdout": PUSH_OUTPUT}),
        ("echo 'git push'", {"stdout": PUSH_OUTPUT}),
        ("git push --dry-run", {"stderr": PUSH_OUTPUT}),
        ("git push --dry-r", {"stderr": PUSH_OUTPUT}),
        ("git push --quiet", {"stdout": PUSH_OUTPUT}),
        ("git push -qf", {"stdout": PUSH_OUTPUT}),
        ("git push --del origin feature/topic", {"stderr": PUSH_OUTPUT}),
        ("git push -fn origin feature/topic", {"stderr": PUSH_OUTPUT}),
        ("git push --delete origin feature/topic", {"stderr": PUSH_OUTPUT}),
        ("git push origin :feature/topic", {"stderr": PUSH_OUTPUT}),
        ("git push --tags", {"stderr": PUSH_OUTPUT}),
        ("git push origin HEAD:refs/tags/v1", {"stderr": PUSH_OUTPUT}),
        ("git push origin HEAD:refs/notes/commits", {"stderr": PUSH_OUTPUT}),
        ("git push origin HEAD:refs/custom/topic", {"stderr": PUSH_OUTPUT}),
        ("git push || true", {"stderr": PUSH_OUTPUT}),
        ("git push | cat", {"stdout": PUSH_OUTPUT}),
        ("git push &", {"stdout": PUSH_OUTPUT}),
        ("git push", {"stderr": PUSH_OUTPUT, "exit_code": 1}),
        ("git push", {"stderr": PUSH_OUTPUT, "session_id": 12, "exit_code": None}),
        (
            "git push",
            {
                "stderr": "To github.com:contributor/project.git\n"
                " ! [rejected] local -> feature/topic\n"
            },
        ),
        ("git push", {"stderr": PUSH_OUTPUT + "error: failed to push some refs\n"}),
        ("git push", {"stderr": "Everything up-to-date\n"}),
        (
            "git push && gh pr view",
            {"stderr": "Everything up-to-date\n", "stdout": PUSH_OUTPUT},
        ),
        ("git push && gh pr view", {"stderr": "", "stdout": PUSH_OUTPUT}),
        ("git push && gh pr view", "Everything up-to-date\n" + PUSH_OUTPUT),
        ("git push", {"stderr": ""}),
        ("git push", {"stderr": PUSH_OUTPUT * 2}),
        ("git push", {"stderr": PUSH_OUTPUT.replace("github.com", "gitlab.com")}),
        ("git push", {"stderr": PUSH_OUTPUT.replace("abc1234..def5678", "[new tag]")}),
        ("git push", {"stderr": PUSH_OUTPUT.replace("feature/topic", "refs/notes/commits")}),
    ],
)
def test_unconfirmed_pushes_do_not_query_github(
    monkeypatch: pytest.MonkeyPatch, command: str, result: object
) -> None:
    gh = Mock(side_effect=AssertionError("No GitHub lookup for an unconfirmed push"))
    monkeypatch.setattr(github_resource, "_gh", gh)
    assert pushed_prs(_shell_segments(command), result) == []
    gh.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        (1, "denied", ""),
        (None, "", "timed out"),
        (0, "bad JSON", ""),
        (0, "{}", ""),
        (0, "[null, {}]", ""),
    ],
)
def test_lookup_failure_does_not_associate(
    monkeypatch: pytest.MonkeyPatch, response: tuple[int | None, str, str]
) -> None:
    monkeypatch.setattr(github_resource, "_gh", Mock(return_value=response))
    assert pushed_prs(_shell_segments("git push"), {"stderr": PUSH_OUTPUT}) == []


def test_enterprise_uses_reported_host_and_ignores_unrelated_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    url = PR_URL.replace("github.com", "ghe.example.com")
    gh = Mock(
        return_value=(
            0,
            json.dumps(
                [
                    {
                        "state": "open",
                        "head": {
                            "ref": "feature/topic",
                            "repo": {"full_name": "contributor/project"},
                        },
                        "html_url": url,
                    }
                ]
            ),
            "",
        )
    )
    monkeypatch.setattr(github_resource, "_gh", gh)
    result = {
        "stderr": PUSH_OUTPUT.replace("github.com", "ghe.example.com"),
        "stdout": PUSH_OUTPUT,
    }
    assert [
        reference.url
        for reference in pushed_prs(_shell_segments("git push && gh pr view"), result)
    ] == [url]
    assert gh.call_args.args[0][2] == "ghe.example.com"


def test_claude_merged_streams_track_pushes_but_not_pr_body_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = Mock(
        return_value=(
            0,
            json.dumps(
                [
                    {
                        "state": "open",
                        "head": {
                            "ref": "feature/topic",
                            "repo": {"full_name": "contributor/project"},
                        },
                        "html_url": PR_URL,
                    }
                ]
            ),
            "",
        )
    )
    monkeypatch.setattr(github_resource, "_gh", gh)
    segments = _shell_segments("cd repo2 && git push && gh pr view")
    response = {
        "stdout": PUSH_OUTPUT + "title: PR body\n   deadbee..cafebabe x -> other\n",
        "stderr": "",
        "interrupted": False,
    }
    assert [reference.url for reference in pushed_prs(segments, response)] == [PR_URL]
    gh.assert_called_once()
    gh.reset_mock()
    response["stdout"] = "Everything up-to-date\n" + PUSH_OUTPUT
    assert pushed_prs(segments, response) == []
    gh.assert_not_called()


@pytest.mark.parametrize("deadline_expires", [False, True])
def test_multiple_repositories_share_one_lookup_deadline(
    monkeypatch: pytest.MonkeyPatch, deadline_expires: bool
) -> None:
    urls = [PR_URL, "https://github.com/other/project/pull/8"]
    gh = Mock(
        side_effect=[
            (
                0,
                json.dumps(
                    [
                        {
                            "state": "open",
                            "head": {"ref": "feature/topic", "repo": {"full_name": repository}},
                            "html_url": url,
                        }
                    ]
                ),
                "",
            )
            for repository, url in zip(("contributor/project", "other/project"), urls, strict=True)
        ]
    )
    monkeypatch.setattr(github_resource, "_gh", gh)
    ticks = iter([0.0, 0.0, 6.0 if deadline_expires else 1.0])
    monkeypatch.setattr(github_push.time, "monotonic", lambda: next(ticks))
    output = PUSH_OUTPUT + PUSH_OUTPUT.replace("contributor/project", "other/project")
    segments = _shell_segments("git -C /repo1 push && git -C /repo2 push")
    assert [reference.url for reference in pushed_prs(segments, {"stderr": output})] == (
        urls[:1] if deadline_expires else urls
    )
    assert gh.call_count == (1 if deadline_expires else 2)


def test_serialized_shell_envelope_reaches_the_push_lookup_decoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sys_os_shell`` serializes its envelope; git's status lines arrive in its stderr."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    row = {
        "state": "open",
        "head": {"ref": "feature/topic", "repo": {"full_name": "contributor/project"}},
        "html_url": PR_URL,
    }
    monkeypatch.setattr(github_resource, "_gh", Mock(return_value=(0, json.dumps([row]), "")))
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        observe_tool_completion(
            "conv_push",
            tool_name="sys_os_shell",
            arguments={"command": "git push origin local:feature/topic"},
            result=json.dumps({"stdout": "", "stderr": PUSH_OUTPUT, "exit_code": 0}),
            call_id="push",
        )
    entries = SessionPrRegistry("conv_push").list()
    assert [(entry.url, entry.relationship) for entry in entries] == [(PR_URL, "worked_on")]
