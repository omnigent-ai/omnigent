"""Pushes are found from git's status output and from the checkout's reflog."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.runner import github_resource, pr_observer
from omnigent.runner.git_providers import github_push, push_reflog
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


def _git(*argv: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout


def _report(directories: list[str]) -> list[tuple[str, str]]:
    """Return ``(url, branch)`` for recent pushes, marking them reported as a lookup would."""
    pushes = push_reflog.recent_pushes(directories)
    push_reflog.mark_reported(push.key for push in pushes)
    return [(push.url, push.branch) for push in pushes]


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout on branch ``local`` whose ``origin`` is a local bare repository."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    _git("init", "-q", "-b", "local", str(work), cwd=tmp_path)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("commit", "-q", "--allow-empty", "-m", "change", cwd=work)
    return remote, work


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git shipit 2>&1 | tail -1", id="alias-piped"),
        pytest.param("git push -q origin HEAD:feature/topic", id="quiet"),
        pytest.param("sh push.sh > /dev/null 2>&1", id="script-redirected"),
    ],
)
def test_reflog_reports_pushes_the_output_cannot_show(
    checkout: tuple[Path, Path], command: str
) -> None:
    remote, work = checkout
    _git("config", "alias.shipit", "push origin HEAD:feature/topic", cwd=work)
    (work / "push.sh").write_text("git push origin HEAD:feature/topic\n")
    subprocess.run(command, shell=True, cwd=work, check=True, capture_output=True)
    assert _report([str(work)]) == [(str(remote), "feature/topic")]
    # A later call in the same checkout does not report the push again.
    assert _report([str(work)]) == []


def test_reflog_ignores_pushes_this_checkout_did_not_make(
    checkout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, work = checkout
    _git("push", "-q", "--dry-run", "origin", "HEAD:feature/dry-run", cwd=work)
    assert _report([str(work)]) == []
    # Another worktree of the same repository pushes a different commit.
    other = work.parent / "other"
    _git("worktree", "add", "-q", "-b", "other", str(other), cwd=work)
    _git("commit", "-q", "--allow-empty", "-m", "other", cwd=other)
    _git("push", "-q", "origin", "HEAD:feature/other", cwd=other)
    assert _report([str(work)]) == []
    # Checking out someone else's pushed branch only fetches it.
    clone = work.parent / "clone"
    _git("clone", "-q", str(remote), str(clone), cwd=work.parent)
    _git("commit", "-q", "--allow-empty", "-m", "theirs", cwd=clone)
    _git("push", "-q", "origin", "HEAD:feature/theirs", cwd=clone)
    _git("fetch", "-q", "origin", cwd=work)
    _git("checkout", "-q", "origin/feature/theirs", cwd=work)
    assert _report([str(work)]) == []
    # A push made long before the call is not this call's.
    _git("checkout", "-q", "local", cwd=work)
    _git("push", "-q", "origin", "HEAD:feature/old", cwd=work)
    later = push_reflog.time.time() + push_reflog.RECENT_PUSH_SECONDS + 60
    monkeypatch.setattr(push_reflog.time, "time", lambda: later)
    assert _report([str(work)]) == []


@pytest.fixture
def checked_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Directories the observer asks the reflog about; each reports one push with an open PR."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    checked: list[str] = []

    def recent_pushes(candidates: object) -> list[push_reflog.RecentPush]:
        checked.extend(candidates)  # type: ignore[call-overload]
        return [
            push_reflog.RecentPush(
                "git@github.com:contributor/project.git", "feature/topic", ("repo", "ref", "x", 0)
            )
        ]

    monkeypatch.setattr(github_push, "recent_pushes", recent_pushes)
    row = {
        "state": "open",
        "head": {"ref": "feature/topic", "repo": {"full_name": "contributor/project"}},
        "html_url": PR_URL,
    }
    monkeypatch.setattr(github_resource, "_gh", Mock(return_value=(0, json.dumps([row]), "")))
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
    yield checked
    pool.shutdown(wait=True)


@pytest.mark.parametrize(
    "cwd,arguments,directories",
    [
        pytest.param(
            "/work/repo", {"command": "git pp 2>&1 | tail -3"}, ["/work/repo"], id="hook-cwd"
        ),
        pytest.param(
            "/work", {"command": "cd repo && git pp | tail -3"}, ["/work/repo"], id="relative-cd"
        ),
        pytest.param(
            None,
            {"command": "git -C /work/repo pp && echo done"},
            ["/work/repo"],
            id="git-C-without-cwd",
        ),
        pytest.param(None, {"command": "git pp"}, [], id="no-directory"),
        # An explicit workdir is where the command ran, relative to the hook cwd.
        pytest.param(
            "/work", {"cmd": "./push.sh", "workdir": "repo"}, ["/work/repo"], id="relative-workdir"
        ),
        # Output attribution rejects OR-lists, but their checkouts still get checked.
        pytest.param(
            "/work/repo", {"command": "git pp || true"}, ["/work/repo"], id="or-list-hook-cwd"
        ),
        pytest.param(
            "/work",
            {"command": "cd repo && git pp || echo failed"},
            ["/work", "/work/repo"],
            id="or-list-relative-cd",
        ),
        pytest.param(
            None,
            {"command": "git -C /work/repo pp || true"},
            ["/work/repo"],
            id="or-list-git-C-without-cwd",
        ),
    ],
)
def test_native_hook_checks_the_directories_the_command_ran_in(
    checked_directories: list[str],
    cwd: str | None,
    arguments: dict[str, str],
    directories: list[str],
) -> None:
    pr_observer.observe_hook(
        "conv_hook",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "exec_command" if "cmd" in arguments else "Bash",
            "tool_input": arguments,
            "tool_response": {"stdout": "done\n", "stderr": "", "interrupted": False},
            "tool_use_id": "toolu_push",
            "cwd": cwd,
        },
    )
    pr_observer._lookup_pool.shutdown(wait=True)
    assert list(dict.fromkeys(checked_directories)) == directories
    entries = SessionPrRegistry("conv_hook").list()
    assert [entry.url for entry in entries] == ([PR_URL] if directories else [])


def test_runner_shell_checks_the_directory_it_reports_running_in(
    checked_directories: list[str],
) -> None:
    """A configured ``os_env.cwd`` with no runner workspace still names the checkout."""
    observe_tool_completion(
        "conv_shell",
        tool_name="sys_os_shell",
        arguments={"command": "./push.sh"},
        result=json.dumps({"stdout": "", "stderr": "", "exit_code": 0, "cwd": "/repo"}),
        call_id="shell_push",
        source="runner_shell",
    )
    pr_observer._lookup_pool.shutdown(wait=True)
    assert checked_directories == ["/repo"]
    assert [entry.url for entry in SessionPrRegistry("conv_shell").list()] == [PR_URL]


def test_reflog_reports_distinct_pushes_to_one_branch_in_the_same_second(
    checkout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, work = checkout
    other = work.parent / "other"
    _git("worktree", "add", "-q", "-b", "other", str(other), cwd=work)
    _git("commit", "-q", "--allow-empty", "-m", "other", cwd=other)
    # Git stamps reflog entries with the committer date, so both land in one second.
    monkeypatch.setenv("GIT_COMMITTER_DATE", f"@{int(push_reflog.time.time())} +0000")
    _git("push", "-q", "origin", "HEAD:feature/shared", cwd=work)
    assert _report([str(work)]) == [(str(remote), "feature/shared")]
    _git("push", "-q", "--force", "origin", "HEAD:feature/shared", cwd=other)
    assert _report([str(other)]) == [(str(remote), "feature/shared")]
    assert _report([str(other)]) == []


def test_identical_runner_shell_calls_record_each_pushed_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    url = "git@github.com:contributor/project.git"
    pushes = iter(
        [
            [push_reflog.RecentPush(url, "feature/a", ("repo", "a", "x", 0))],
            [push_reflog.RecentPush(url, "feature/b", ("repo", "b", "y", 0))],
        ]
    )
    monkeypatch.setattr(github_push, "recent_pushes", lambda _directories: next(pushes))
    urls = {
        branch: f"https://github.com/contributor/project/pull/{number}"
        for branch, number in (("feature/a", 1), ("feature/b", 2))
    }

    def gh(argv: list[str], **_: object) -> tuple[int, str, str]:
        branch = argv[-1].split("/commits/")[1].split("/pulls")[0].replace("%2F", "/")
        head = {"ref": branch, "repo": {"full_name": "contributor/project"}}
        return 0, json.dumps([{"state": "open", "head": head, "html_url": urls[branch]}]), ""

    monkeypatch.setattr(github_resource, "_gh", gh)
    result = json.dumps({"stdout": "", "stderr": "", "exit_code": 0, "cwd": "/repo"})
    for _ in range(2):
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
            observe_tool_completion(
                "conv_quiet",
                tool_name="sys_os_shell",
                arguments={"command": "./push.sh"},
                result=result,
                source="runner_shell",
            )
    entries = SessionPrRegistry("conv_quiet").list()
    assert sorted(entry.url for entry in entries) == sorted(urls.values())


def test_reflog_read_does_not_run_a_configured_signature_verifier(
    checkout: tuple[Path, Path], tmp_path: Path
) -> None:
    remote, work = checkout
    marker = tmp_path / "verifier-ran"
    verifier = tmp_path / "verifier.sh"
    verifier.write_text(f"#!/bin/sh\ntouch {marker}\n")
    verifier.chmod(0o755)
    # An agent can write the checkout's config and commit a signature header.
    tree = _git("hash-object", "-t", "tree", "/dev/null", cwd=work).strip()
    head = _git("rev-parse", "HEAD", cwd=work).strip()
    signed = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=work,
        input=(
            f"tree {tree}\nparent {head}\n"
            "author Test <test@example.com> 1700000000 +0000\n"
            "committer Test <test@example.com> 1700000000 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n -----END PGP SIGNATURE-----\n"
            "\nsigned\n"
        ),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git("reset", "-q", "--hard", signed, cwd=work)
    _git("push", "-q", "origin", "HEAD:feature/signed", cwd=work)
    _git("config", "log.showSignature", "true", cwd=work)
    _git("config", "gpg.program", str(verifier), cwd=work)
    assert _report([str(work)]) == [(str(remote), "feature/signed")]
    assert not marker.exists()


def test_failed_lookup_leaves_the_push_for_a_later_call(
    checkout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = checkout
    _git("push", "-q", "origin", "HEAD:feature/topic", cwd=work)
    # Report the push as going to GitHub; the push itself went to the local remote.
    _git("config", "remote.origin.pushurl", "git@github.com:contributor/project.git", cwd=work)
    row = {
        "state": "open",
        "head": {"ref": "feature/topic", "repo": {"full_name": "contributor/project"}},
        "html_url": PR_URL,
    }
    gh = Mock(side_effect=[(1, "", "unavailable"), (0, json.dumps([row]), "")])
    monkeypatch.setattr(github_resource, "_gh", gh)
    segments = pr_observer._with_directories(_shell_segments("./push.sh"), str(work))
    result = {"stdout": "", "stderr": "", "exit_code": 0}
    assert pushed_prs(segments, result) == []
    assert [reference.url for reference in pushed_prs(segments, result)] == [PR_URL]
    assert pushed_prs(segments, result) == []
    assert gh.call_count == 2


def test_reflog_checks_every_directory_the_command_ran_in(
    checkout: tuple[Path, Path], tmp_path: Path
) -> None:
    remote, work = checkout
    elsewhere = [tmp_path / name for name in "abcd"]
    for directory in elsewhere:
        directory.mkdir()
    _git("push", "-q", "origin", "HEAD:feature/topic", cwd=work)
    directories = [*map(str, elsewhere), str(work)]
    assert _report(directories) == [(str(remote), "feature/topic")]
