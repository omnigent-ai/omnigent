"""Push status identifies the updated remote branch without inspecting the checkout."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from filelock import Timeout as FileLockTimeout

from omnigent.runner import github_resource, pr_observer
from omnigent.runner.git_providers import github_push
from omnigent.runner.git_providers.github_push import push_operation, pushed_prs
from omnigent.runner.pr_observer import _shell_segments, observe_hook, observe_tool_completion
from omnigent.runner.session_prs import SessionPrRegistry
from tests._helpers.github_remote import git, github_checkout

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


CHECKOUT_REPO = "contributor/project"
CHECKOUT_PR_URL = f"https://github.com/{CHECKOUT_REPO}/pull/42"
CHECKOUT_PR = {
    "number": 42,
    "state": "open",
    "html_url": CHECKOUT_PR_URL,
    "head": {"ref": "topic", "repo": {"full_name": CHECKOUT_REPO}},
    "base": {"repo": {"full_name": CHECKOUT_REPO}},
}
# The PR write prints nothing, so only the checkout names its repository.
GH = """#!/bin/sh
[ "$1" = api ] && echo '{"content": {"path": "notes.txt"}, "commit": {"sha": "cccc"}}'
exit 0
"""


def _github(args: list[str], **_: object) -> tuple[int, str, str]:
    if args[-1] == f"repos/{CHECKOUT_REPO}/commits/topic/pulls":
        return 0, json.dumps([CHECKOUT_PR]), ""
    if args[-1] == f"repos/{CHECKOUT_REPO}/pulls/42":
        return 0, json.dumps(CHECKOUT_PR), ""
    return 1, "", "not found"


@pytest.fixture
def observed_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(pr_observer, "_pushes", pr_observer.pushed_branches.SessionPushes())
    monkeypatch.setattr(github_resource, "_gh", Mock(side_effect=_github))
    return "conv_write"


@pytest.mark.parametrize("harness", ["sys_os_shell", "claude_native", "codex_native"])
@pytest.mark.parametrize(
    "write",
    [
        pytest.param("git push origin HEAD:topic", id="literal-push"),
        pytest.param("git pp 2>&1 | grep -v Bloom | tail -15", id="alias-with-pipe"),
        pytest.param("make push", id="make-wrapper"),
        pytest.param("git push origin HEAD:topic 2>&1", id="redirected-push"),
        pytest.param("git push -q -u origin topic", id="quiet-push"),
        pytest.param("gh pr ready 42", id="pr-ready-without-repo"),
        pytest.param(
            f"gh api -X PUT repos/{CHECKOUT_REPO}/contents/notes.txt"
            " -f message='Update notes' -f content=bm90ZXMK -f branch=topic",
            id="gh-api-contents-write",
        ),
    ],
)
def test_branch_write_in_a_github_checkout_tracks_its_open_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_session: str,
    harness: str,
    write: str,
) -> None:
    workspace = tmp_path / "workspace"
    worktree = workspace / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("config", "alias.pp", "push origin HEAD", cwd=worktree)
    (worktree / "Makefile").write_text("push:\n\tgit push origin HEAD\n")
    git("add", "Makefile", cwd=worktree)
    git("commit", "-qm", "Add push target", cwd=worktree)
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "gh").write_text(GH)
    (binary / "gh").chmod(0o755)
    command = f"cd {shlex.quote(str(worktree))} && {write}"
    completed = subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={**os.environ, "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}"},
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        if harness == "sys_os_shell":
            observe_tool_completion(
                observed_session,
                tool_name="sys_os_shell",
                arguments={"command": command},
                result=json.dumps(
                    {
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                        "exit_code": completed.returncode,
                    }
                ),
                call_id="write",
            )
        else:
            claude = harness == "claude_native"
            observe_hook(
                observed_session,
                {
                    "hook_event_name": "PostToolUse",
                    "cwd": str(workspace),
                    "tool_use_id": "write",
                    "tool_name": "Bash" if claude else "exec_command",
                    "tool_input": {"command" if claude else "cmd": command},
                    "tool_response": (
                        {
                            "stdout": completed.stdout + completed.stderr,
                            "stderr": "",
                            "interrupted": False,
                            "isImage": False,
                            "noOutputExpected": False,
                        }
                        if claude
                        else {
                            "stdout": completed.stdout,
                            "stderr": completed.stderr,
                            "exit_code": completed.returncode,
                        }
                    ),
                },
            )
    entries = SessionPrRegistry(observed_session).list()
    assert [(entry.url, entry.relationship) for entry in entries] == [
        (CHECKOUT_PR_URL, "worked_on")
    ]


PUSH_FILES = {
    "owner": "contributor",
    "repo": "project",
    "branch": "topic",
    "files": [{"path": "notes.txt", "content": "notes\n"}],
    "message": "Update notes",
}
PUSHED_REF = {"ref": "refs/heads/topic", "object": {"sha": "e" * 40, "type": "commit"}}


@pytest.mark.parametrize(
    "tool_name,arguments,result",
    [
        pytest.param("github__push_files", PUSH_FILES, json.dumps(PUSHED_REF), id="push-files"),
        pytest.param(
            "mcp__github__push_files",
            PUSH_FILES,
            {"content": [{"type": "text", "text": json.dumps(PUSHED_REF)}]},
            id="claude-push-files",
        ),
        pytest.param(
            "github__create_or_update_file",
            {
                "owner": "contributor",
                "repo": "project",
                "branch": "topic",
                "path": "notes.txt",
                "content": "notes\n",
                "message": "Update notes",
            },
            json.dumps({"content": {"path": "notes.txt"}, "commit": {"sha": "c" * 40}}),
            id="create-or-update-file",
        ),
    ],
)
def test_mcp_branch_write_tracks_its_open_pr(
    monkeypatch: pytest.MonkeyPatch,
    observed_session: str,
    tool_name: str,
    arguments: dict[str, object],
    result: object,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        observe_tool_completion(
            observed_session,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            call_id="write",
        )
    entries = SessionPrRegistry(observed_session).list()
    assert [(entry.url, entry.relationship) for entry in entries] == [
        (CHECKOUT_PR_URL, "worked_on")
    ]


UPSTREAM_PR_URL = "https://github.com/upstream/project/pull/7"


@dataclass
class _Forge:
    """GitHub's answers by API endpoint, and the endpoints the observer asked for."""

    responses: dict[str, object] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)

    def gh(self, args: list[str], **_: object) -> tuple[int, str, str]:
        self.lookups.append(args[-1])
        if args[-1] in self.responses:
            return 0, json.dumps(self.responses[args[-1]]), ""
        return 1, "", "not found"


@pytest.fixture
def forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Forge:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(pr_observer, "_pushes", pr_observer.pushed_branches.SessionPushes())
    stub = _Forge()
    monkeypatch.setattr(github_resource, "_gh", Mock(side_effect=stub.gh))
    return stub


def _branch_pr(url: str, repository: str, branch: str, created_at: str) -> dict[str, object]:
    return {
        "state": "open",
        "html_url": url,
        "created_at": created_at,
        "head": {"ref": branch, "repo": {"full_name": repository}},
    }


def _observe_shell(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    command: str,
    result: dict[str, object],
    **options: object,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        observe_tool_completion(
            session_id,
            tool_name="sys_os_shell",
            arguments={"command": command},
            result=json.dumps(result),
            call_id=uuid.uuid4().hex,
            **options,
        )


def _run_and_observe(
    monkeypatch: pytest.MonkeyPatch, session_id: str, command: str, cwd: Path
) -> None:
    """Run *command* in *cwd* like ``sys_os_shell`` and observe it with its time window."""
    started_at = time.time()
    completed = subprocess.run(
        ["/bin/sh", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    result = {
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
        "cwd": str(cwd),
    }
    _observe_shell(monkeypatch, session_id, command, result, started_at=started_at)


def _tracked(session_id: str) -> list[tuple[str, str]]:
    return [(entry.url, entry.relationship) for entry in SessionPrRegistry(session_id).list()]


def test_push_without_upstream_to_a_fork_tracks_the_upstream_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    worktree = tmp_path / "worktree"
    remotes = tmp_path / "github"
    github_checkout(worktree, remotes, "upstream/project", "main")
    git("init", "-q", "--bare", str(remotes / f"{CHECKOUT_REPO}.git"))
    git("remote", "add", "fork", f"git@github.com:{CHECKOUT_REPO}.git", cwd=worktree)
    git("checkout", "-qb", "topic", cwd=worktree)
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [
        _branch_pr(UPSTREAM_PR_URL, CHECKOUT_REPO, "topic", "2020-01-01T00:00:00Z")
    ]
    _run_and_observe(monkeypatch, "conv_fork", "git push fork topic 2>&1 | tail -1", worktree)
    assert git("rev-parse", "topic", cwd=remotes / f"{CHECKOUT_REPO}.git") == git(
        "rev-parse", "HEAD", cwd=worktree
    )
    assert _tracked("conv_fork") == [(UPSTREAM_PR_URL, "worked_on")]


@pytest.mark.parametrize("found_by", ["first-lookup", "turn-end-recheck"])
@pytest.mark.parametrize(
    "opened_after_start,relationship",
    [
        pytest.param(5.0, "created", id="opened-during-the-call"),
        pytest.param(-60.0, "worked_on", id="opened-earlier"),
    ],
)
def test_pr_opened_during_the_pushing_call_counts_as_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forge: _Forge,
    opened_after_start: float,
    relationship: str,
    found_by: str,
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    started_at = time.time() - 10
    git("push", "-q", cwd=worktree)
    opened = datetime.fromtimestamp(started_at + opened_after_start, UTC).isoformat()
    pulls = [_branch_pr(CHECKOUT_PR_URL, CHECKOUT_REPO, "topic", opened)]
    if found_by == "first-lookup":
        forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = pulls
    result = {"stdout": "", "stderr": "", "exit_code": 0, "cwd": str(worktree)}
    _observe_shell(
        monkeypatch,
        "conv_created",
        "git push -q",
        result,
        started_at=started_at,
        finished_at=started_at + 20,
    )
    if found_by == "turn-end-recheck":
        assert _tracked("conv_created") == []
        forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = pulls
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
            pr_observer.recheck_pending("conv_created")
    assert _tracked("conv_created") == [(CHECKOUT_PR_URL, relationship)]


@pytest.mark.parametrize(
    "command,rejects",
    [
        pytest.param("git push --dry-run origin HEAD:topic", False, id="dry-run"),
        pytest.param("git push origin HEAD:topic 2>&1 | tail -3", True, id="rejected"),
        pytest.param("{elsewhere} && git status --short", False, id="other-worktree-pushes"),
        pytest.param(
            "git fetch -q origin review && git checkout -q --track origin/review",
            False,
            id="pr-checkout",
        ),
    ],
)
def test_call_that_pushes_no_head_of_its_checkouts_looks_up_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge, command: str, rejects: bool
) -> None:
    worktree = tmp_path / "worktree"
    remote = github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    if rejects:
        (remote / "hooks" / "pre-receive").write_text("#!/bin/sh\nexit 1\n")
        (remote / "hooks" / "pre-receive").chmod(0o755)
    other = tmp_path / "other"
    git("worktree", "add", "-qb", "other", str(other), cwd=worktree)
    git("commit", "-q", "--allow-empty", "-m", "Elsewhere", cwd=other)
    # Stands in for another agent pushing from a sibling worktree during the call.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(f"#!/bin/sh\ngit -C {shlex.quote(str(other))} push -q origin other\n")
    elsewhere.chmod(0o755)
    review = git(
        "-c", "user.name=Reviewer", "-c", "user.email=reviewer@example.com",
        "commit-tree", "-p", "topic", "-m", "Review", "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        cwd=remote,
    )  # fmt: skip
    git("update-ref", "refs/heads/review", review, cwd=remote)
    command = command.format(elsewhere=shlex.quote(str(elsewhere)))
    _run_and_observe(monkeypatch, "conv_quiet", command, worktree)
    _run_and_observe(monkeypatch, "conv_quiet", "ls", worktree)
    if "elsewhere" in command:
        assert git("rev-parse", "origin/other", cwd=other) == git("rev-parse", "HEAD", cwd=other)
    assert forge.lookups == []
    assert _tracked("conv_quiet") == []


@pytest.mark.parametrize(
    "command,found",
    [
        pytest.param("{pusher}", False, id="known-directory"),
        pytest.param(
            'export REPO={worktree}; cd "$REPO" && git push -q origin HEAD:topic',
            True,
            id="directory-only-the-shell-knows",
        ),
    ],
)
def test_checkout_used_earlier_is_read_only_when_the_call_directory_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge, command: str, found: bool
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [
        _branch_pr(CHECKOUT_PR_URL, CHECKOUT_REPO, "topic", "2020-01-01T00:00:00Z")
    ]
    _run_and_observe(monkeypatch, "conv_elsewhere", "git status --short", worktree)
    # Stands in for a push from another terminal in the checkout during the call.
    pusher = tmp_path / "pusher"
    pusher.write_text(
        f"#!/bin/sh\ngit -C {shlex.quote(str(worktree))} push -q origin HEAD:topic\n"
    )
    pusher.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    command = command.format(pusher=shlex.quote(str(pusher)), worktree=shlex.quote(str(worktree)))
    _run_and_observe(monkeypatch, "conv_elsewhere", command, scratch)
    assert git("rev-parse", "origin/topic", cwd=worktree) == git("rev-parse", "HEAD", cwd=worktree)
    assert _tracked("conv_elsewhere") == ([(CHECKOUT_PR_URL, "worked_on")] if found else [])


@pytest.mark.parametrize("later", ["next-call", "turn-end"])
def test_pushed_branch_without_a_pr_is_rechecked_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge, later: str
) -> None:
    if later == "next-call":
        monkeypatch.setattr(pr_observer.pushed_branches, "RECHECK_INTERVAL_S", 0.0)
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    _run_and_observe(monkeypatch, "conv_late", "git push -q", worktree)
    assert _tracked("conv_late") == []
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [
        _branch_pr(CHECKOUT_PR_URL, CHECKOUT_REPO, "topic", "2020-01-01T00:00:00Z")
    ]
    if later == "next-call":
        _run_and_observe(monkeypatch, "conv_late", "ls", worktree)
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
            pr_observer.recheck_pending("conv_late")
    assert _tracked("conv_late") == [(CHECKOUT_PR_URL, "worked_on")]


def test_turn_end_recheck_during_the_first_lookup_finds_a_pr_opened_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    first_lookup, release = threading.Event(), threading.Event()

    def gh(args: list[str], **options: object) -> tuple[int, str, str]:
        if first_lookup.is_set():
            return forge.gh(args, **options)
        first_lookup.set()
        release.wait(30)
        return 1, "", "not found"

    monkeypatch.setattr(github_resource, "_gh", Mock(side_effect=gh))
    started_at = time.time()
    git("push", "-q", cwd=worktree)
    result = {"stdout": "", "stderr": "", "exit_code": 0, "cwd": str(worktree)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        try:
            observe_tool_completion(
                "conv_race",
                tool_name="sys_os_shell",
                arguments={"command": "git push -q"},
                result=json.dumps(result),
                call_id="push",
                started_at=started_at,
            )
            assert first_lookup.wait(30)
            forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [
                _branch_pr(CHECKOUT_PR_URL, CHECKOUT_REPO, "topic", "2020-01-01T00:00:00Z")
            ]
            pr_observer.recheck_pending("conv_race")
        finally:
            release.set()
    assert _tracked("conv_race") == [(CHECKOUT_PR_URL, "worked_on")]


def test_pr_found_while_the_registry_is_busy_is_recorded_at_turn_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [
        _branch_pr(CHECKOUT_PR_URL, CHECKOUT_REPO, "topic", "2020-01-01T00:00:00Z")
    ]
    record = SessionPrRegistry.record
    attempts: list[object] = []

    def busy(self: SessionPrRegistry, references: list[object], **options: object) -> None:
        if references:
            attempts.append(references)
            raise FileLockTimeout("session-prs.json.lock")
        record(self, references, **options)

    monkeypatch.setattr(SessionPrRegistry, "record", busy)
    _run_and_observe(monkeypatch, "conv_busy", "git push -q", worktree)
    assert attempts
    assert _tracked("conv_busy") == []
    monkeypatch.setattr(SessionPrRegistry, "record", record)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        pr_observer.recheck_pending("conv_busy")
    assert _tracked("conv_busy") == [(CHECKOUT_PR_URL, "worked_on")]


@pytest.mark.parametrize(
    "command,inherited,resolved,repository",
    [
        pytest.param("gh pr ready 42", None, None, "upstream/project", id="upstream-remote"),
        pytest.param("gh pr ready 42", None, "base", CHECKOUT_REPO, id="gh-resolved-default"),
        pytest.param(
            "export GH_REPO=other/repo && gh pr ready 42", None, None, None, id="exported-gh-repo"
        ),
        pytest.param("gh pr ready 42", "other/repo", None, None, id="inherited-gh-repo"),
    ],
)
def test_numbered_pr_write_without_repo_uses_the_checkout_base_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forge: _Forge,
    command: str,
    inherited: str | None,
    resolved: str | None,
    repository: str | None,
) -> None:
    if inherited:
        monkeypatch.setenv("GH_REPO", inherited)
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("remote", "add", "upstream", "git@github.com:upstream/project.git", cwd=worktree)
    if resolved:
        git("config", "remote.origin.gh-resolved", resolved, cwd=worktree)
    for candidate in ("upstream/project", CHECKOUT_REPO):
        forge.responses[f"repos/{candidate}/pulls/42"] = {
            **CHECKOUT_PR,
            "html_url": f"https://github.com/{candidate}/pull/42",
        }
    result = {"stdout": "", "stderr": "", "exit_code": 0, "cwd": str(worktree)}
    _observe_shell(monkeypatch, "conv_ready", command, result)
    url = f"https://github.com/{repository}/pull/42"
    assert _tracked("conv_ready") == ([(url, "worked_on")] if repository else [])


@pytest.mark.parametrize(
    "visible,reports_cwd",
    [
        pytest.param(True, True, id="readable-checkout"),
        pytest.param(False, True, id="copy-on-write-workspace"),
        pytest.param(True, False, id="unknown-directory"),
    ],
)
def test_push_output_finds_pushes_the_checkout_state_does_not_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forge: _Forge,
    visible: bool,
    reports_cwd: bool,
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [CHECKOUT_PR]
    url = f"https://github.com/{CHECKOUT_REPO}.git"
    # A push to a URL that matches no remote updates no remote-tracking ref.
    command = f"git push {url} HEAD:topic"
    result: dict[str, object] = {
        "stdout": "",
        "stderr": f"To {url}\n   abc1234..def5678  HEAD -> topic\n",
        "exit_code": 0,
    }
    if reports_cwd:
        result["cwd"] = str(worktree)
    scan = Mock(wraps=pr_observer._scan_pushes)
    monkeypatch.setattr(pr_observer, "_scan_pushes", scan)
    _observe_shell(
        monkeypatch,
        "conv_output",
        command,
        result,
        started_at=time.time(),
        repository_visible=visible,
    )
    assert scan.called is visible
    assert _tracked("conv_output") == [(CHECKOUT_PR_URL, "worked_on")]


def test_push_in_both_output_and_checkout_state_is_looked_up_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [CHECKOUT_PR]
    _run_and_observe(monkeypatch, "conv_once", "git push origin HEAD:topic", worktree)
    assert forge.lookups == [f"repos/{CHECKOUT_REPO}/commits/topic/pulls"]
    assert _tracked("conv_once") == [(CHECKOUT_PR_URL, "worked_on")]


@pytest.mark.parametrize(
    "tool_name,argument", [("Bash", "command"), ("exec_command", "cmd")], ids=["claude", "codex"]
)
def test_native_hook_cwd_locates_a_quiet_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge, tool_name: str, argument: str
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [CHECKOUT_PR]
    command = "git push -q origin HEAD:topic"
    subprocess.run(["/bin/sh", "-c", command], cwd=worktree, check=True, timeout=60)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        observe_hook(
            "conv_hook",
            {
                "hook_event_name": "PostToolUse",
                "cwd": str(worktree),
                "tool_use_id": "push",
                "tool_name": tool_name,
                "tool_input": {argument: command},
                "tool_response": {"stdout": "", "stderr": "", "exit_code": 0},
            },
        )
    assert _tracked("conv_hook") == [(CHECKOUT_PR_URL, "worked_on")]


async def test_native_hook_ignores_a_push_made_before_the_session_relay_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    from omnigent.harnesses.claude_native import bridge

    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [CHECKOUT_PR]
    # A minute before the session: inside the lookback of a hook without a start time.
    subprocess.run(
        ["git", "push", "-q", "origin", "HEAD:topic"],
        cwd=worktree,
        check=True,
        timeout=60,
        env={**os.environ, "GIT_COMMITTER_DATE": f"@{int(time.time()) - 60} +0000"},
    )
    relay = bridge.start_tool_relay(
        bridge_dir=bridge.prepare_bridge_dir("conv_before", workspace=tmp_path),
        tools=[],
        tool_executor=None,
        loop=asyncio.get_running_loop(),
        session_id="conv_before",
    )
    relay.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        observe_hook(
            "conv_before",
            {
                "hook_event_name": "PostToolUse",
                "cwd": str(worktree),
                "tool_use_id": "list",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
            },
        )
    assert forge.lookups == []
    assert _tracked("conv_before") == []


CONTENTS_WRITE = (
    "gh api -X PUT 'repos/{owner}/{repo}/contents/notes.txt'"
    " -f message='Update notes' -f content=bm90ZXMK -f branch=topic"
)
REF_WRITE = "gh api -X PATCH 'repos/{owner}/{repo}/git/refs/heads/{branch}' -f sha=abc1234"


@pytest.mark.parametrize(
    "command,inherited,repository",
    [
        pytest.param(CONTENTS_WRITE, None, CHECKOUT_REPO, id="checkout-repository"),
        pytest.param(
            f"GH_REPO=upstream/project {CONTENTS_WRITE}",
            None,
            "upstream/project",
            id="gh-repo-override",
        ),
        pytest.param(REF_WRITE, None, CHECKOUT_REPO, id="current-branch"),
        pytest.param(
            f"export GH_REPO=upstream/project; {CONTENTS_WRITE}", None, None, id="exported-gh-repo"
        ),
        pytest.param(REF_WRITE, "upstream/project", None, id="inherited-gh-repo"),
    ],
)
def test_gh_api_branch_write_fills_placeholders_as_gh_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forge: _Forge,
    command: str,
    inherited: str | None,
    repository: str | None,
) -> None:
    if inherited:
        monkeypatch.setenv("GH_REPO", inherited)
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    git("commit", "-q", "--allow-empty", "-m", "Change", cwd=worktree)
    for candidate in ("upstream/project", CHECKOUT_REPO):
        forge.responses[f"repos/{candidate}/commits/topic/pulls"] = [
            _branch_pr(
                f"https://github.com/{candidate}/pull/42",
                candidate,
                "topic",
                "2020-01-01T00:00:00Z",
            )
        ]
    result = {"stdout": "{}", "stderr": "", "exit_code": 0, "cwd": str(worktree)}
    _observe_shell(monkeypatch, "conv_placeholder", command, result)
    url = f"https://github.com/{repository}/pull/42"
    assert forge.lookups == ([f"repos/{repository}/commits/topic/pulls"] if repository else [])
    assert _tracked("conv_placeholder") == ([(url, "worked_on")] if repository else [])


@pytest.mark.parametrize("configured", [False, True], ids=["unknown-host", "configured-host"])
def test_branch_write_queries_only_a_known_github_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge, configured: bool
) -> None:
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "ghe.example" if configured else "")
    endpoint = f"repos/{CHECKOUT_REPO}/commits/topic/pulls"
    url = f"https://ghe.example/{CHECKOUT_REPO}/pull/42"
    forge.responses[endpoint] = [_branch_pr(url, CHECKOUT_REPO, "topic", "2020-01-01T00:00:00Z")]
    gh = Mock(side_effect=forge.gh)
    monkeypatch.setattr(github_resource, "_gh", gh)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        observe_tool_completion(
            "conv_host",
            tool_name="github__push_files",
            arguments={**PUSH_FILES, "hostname": "ghe.example"},
            result=json.dumps(PUSHED_REF),
            call_id="write",
        )
    queried = [["api", "--hostname", "ghe.example", endpoint]] if configured else []
    assert [call.args[0] for call in gh.call_args_list] == queried
    assert _tracked("conv_host") == ([(url, "worked_on")] if configured else [])


def test_reading_a_push_runs_no_program_the_checkout_config_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    worktree = tmp_path / "worktree"
    github_checkout(worktree, tmp_path / "github", CHECKOUT_REPO, "topic")
    commit = (
        f"tree {git('rev-parse', 'HEAD^{tree}', cwd=worktree)}\n"
        f"parent {git('rev-parse', 'HEAD', cwd=worktree)}\n"
        "author Contributor <contributor@example.com> 1700000000 +0000\n"
        "committer Contributor <contributor@example.com> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQ==\n -----END PGP SIGNATURE-----\n"
        "\nSigned change\n"
    )
    signed = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=worktree, input=commit, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    git("reset", "-q", "--hard", signed, cwd=worktree)
    marker = tmp_path / "verifier-ran"
    verifier = tmp_path / "verifier"
    verifier.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    verifier.chmod(0o755)
    git("config", "log.showSignature", "true", cwd=worktree)
    git("config", "gpg.program", str(verifier), cwd=worktree)
    forge.responses[f"repos/{CHECKOUT_REPO}/commits/topic/pulls"] = [CHECKOUT_PR]
    _run_and_observe(monkeypatch, "conv_signed", "git push -q", worktree)
    assert _tracked("conv_signed") == [(CHECKOUT_PR_URL, "worked_on")]
    assert not marker.exists()
    git("reflog", "show", "-n", "1", "--format=%gD %gs", "origin/topic", cwd=worktree)
    assert marker.exists()
