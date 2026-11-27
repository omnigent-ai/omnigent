"""Finding a branch's pull requests in the workspace's own repository.

A real git checkout supplies the remotes; only ``gh`` is faked, so the remote resolution,
the host check and the token lookup run as they really do.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner import github_search
from omnigent.runner.pull_request_errors import PullRequestError

REPOSITORY = "example-org/example-repo"
GHES = "code.example.com"
TOKEN = "ghp_example_token_value"


def _entry(number: int, head: str = "feature/login", **fields: Any) -> dict[str, Any]:
    return {
        "url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "number": number,
        "title": f"PR {number}",
        "state": "OPEN",
        "isDraft": False,
        "updatedAt": "2026-01-02T03:04:05Z",
        "headRefName": head,
        **fields,
    }


class FakeGh:
    """Answers each ``gh`` process with ``listed``; git runs for real."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.listed: Any = []
        self.list_exit_code = 0
        self.raw_output: str | None = None
        real_run = github._run

        def run(
            argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
        ) -> tuple[int | None, str, str]:
            if argv[0] == "git":
                return real_run(argv, cwd=cwd, timeout=timeout, env=env)
            assert argv[0] == "gh", argv
            self.calls.append(argv[1:])
            self.envs.append(env)
            if argv[1:3] == ["auth", "token"]:
                return 0, TOKEN + "\n", ""
            assert argv[1:3] == ["pr", "list"], argv
            if self.list_exit_code:
                return self.list_exit_code, "", "HTTP 502: example-org/secret-repo is unreachable"
            if self.raw_output is not None:
                return 0, self.raw_output, ""
            return 0, json.dumps(self.listed), ""

        monkeypatch.setattr(github, "_run", run)

    def list_arguments(self) -> list[str]:
        (argv,) = [call for call in self.calls if call[:2] == ["pr", "list"]]
        return argv


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No stored or configured gh hosts, no GH_HOST, no saved account and no sandbox."""
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", raising=False)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _key: None)
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    return FakeGh(monkeypatch)


def _checkout(tmp_path: Path, **remotes: str) -> str:
    root = tmp_path / "work"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "remote", "add", name, url], cwd=root, check=True)
    return str(root)


def test_a_branch_lists_its_pull_requests_in_the_workspaces_repository(
    tmp_path: Path, fake: FakeGh
) -> None:
    root = _checkout(tmp_path, origin=f"git@github.com:{REPOSITORY}.git")
    fake.listed = [_entry(7), _entry(5, state="MERGED", isDraft=True)]

    found = github_search.branch_pull_requests(root, branch="feature/login")

    assert found == {
        "object": "list",
        "data": [
            {
                "url": f"https://github.com/{REPOSITORY}/pull/7",
                "number": 7,
                "title": "PR 7",
                "state": "OPEN",
                "is_draft": False,
                "updated_at": "2026-01-02T03:04:05Z",
                "head_ref": "feature/login",
            },
            {
                "url": f"https://github.com/{REPOSITORY}/pull/5",
                "number": 5,
                "title": "PR 5",
                "state": "MERGED",
                "is_draft": True,
                "updated_at": "2026-01-02T03:04:05Z",
                "head_ref": "feature/login",
            },
        ],
    }
    argv = fake.list_arguments()
    assert argv[argv.index("-R") + 1] == f"github.com/{REPOSITORY}"
    # One argument, so a branch can never be read as another flag.
    assert "--head=feature/login" in argv
    assert argv[argv.index("--state") + 1] == "all"
    assert argv[argv.index("--limit") + 1] == str(github_search.MAX_BRANCH_PULL_REQUESTS)
    assert fake.calls == [argv]


def test_the_base_remote_gh_resolved_is_listed_before_origin(tmp_path: Path, fake: FakeGh) -> None:
    """A fork's pull requests live in the base repository ``gh repo set-default`` chose."""
    root = _checkout(
        tmp_path,
        origin="https://github.com/someone/example-repo.git",
        upstream=f"https://github.com/{REPOSITORY}.git",
    )
    subprocess.run(["git", "config", "remote.upstream.gh-resolved", "base"], cwd=root, check=True)

    github_search.branch_pull_requests(root, branch="feature/login")

    argv = fake.list_arguments()
    assert argv[argv.index("-R") + 1] == f"github.com/{REPOSITORY}"


@pytest.mark.parametrize(
    "listed",
    [
        # Another repository's, another host's, and one that's no pull request URL.
        [_entry(1, url="https://github.com/example-org/other-repo/pull/1")],
        [_entry(2, url=f"https://{GHES}/{REPOSITORY}/pull/2")],
        [_entry(3, url="javascript:alert(1)")],
        # Another branch's, and entries GitHub never sends.
        [_entry(4, head="main")],
        [_entry(5, url=None)],
        ["not an entry"],
        {"url": f"https://github.com/{REPOSITORY}/pull/6"},
    ],
    ids=[
        "other-repository",
        "other-host",
        "not-a-url",
        "other-branch",
        "no-url",
        "string",
        "object",
    ],
)
def test_an_answer_not_for_this_repository_and_branch_is_dropped(
    tmp_path: Path, fake: FakeGh, listed: Any
) -> None:
    """What GitHub answers is untrusted: a row is shown only for the repository asked about."""
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")
    fake.listed = listed

    assert github_search.branch_pull_requests(root, branch="feature/login")["data"] == []


def test_a_title_that_isnt_text_is_shown_empty(tmp_path: Path, fake: FakeGh) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")
    fake.listed = [_entry(7, title={"html": "<b>x</b>"})]

    (row,) = github_search.branch_pull_requests(root, branch="feature/login")["data"]

    assert row["title"] == ""


def test_the_list_is_capped_whatever_github_answers(tmp_path: Path, fake: FakeGh) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")
    fake.listed = [_entry(number) for number in range(1, 40)]

    rows = github_search.branch_pull_requests(root, branch="feature/login")["data"]

    assert len(rows) == github_search.MAX_BRANCH_PULL_REQUESTS


@pytest.mark.parametrize(
    "remotes",
    [{}, {"origin": "/srv/mirror/example-repo"}],
    ids=["no-remote", "path-remote"],
)
def test_a_workspace_without_a_repository_remote_has_none(
    tmp_path: Path, fake: FakeGh, remotes: dict[str, str]
) -> None:
    root = _checkout(tmp_path, **remotes)

    assert github_search.branch_pull_requests(root, branch="main") == {
        "object": "list",
        "data": [],
    }
    assert fake.calls == []


def test_a_remote_on_a_host_never_set_up_runs_no_gh_with_an_enterprise_token(
    tmp_path: Path, fake: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh would treat every enterprise host as signed in and send it this token."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)
    root = _checkout(tmp_path, origin=f"https://{GHES}/{REPOSITORY}.git")

    with pytest.raises(PullRequestError, match="isn't on a GitHub host set up here"):
        github_search.branch_pull_requests(root, branch="main")

    assert fake.calls == []


def test_a_remote_on_the_gh_host_set_up_is_listed(
    tmp_path: Path, fake: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    root = _checkout(tmp_path, origin=f"https://{GHES}/{REPOSITORY}.git")
    fake.listed = [_entry(7, url=f"https://{GHES}/{REPOSITORY}/pull/7")]

    rows = github_search.branch_pull_requests(root, branch="feature/login")["data"]

    assert [row["url"] for row in rows] == [f"https://{GHES}/{REPOSITORY}/pull/7"]
    argv = fake.list_arguments()
    assert argv[argv.index("-R") + 1] == f"{GHES}/{REPOSITORY}"
    # No sign-in probe: the host was checked without running gh.
    assert [call[:2] for call in fake.calls] == [["pr", "list"]]


def test_the_list_runs_as_the_account_saved_for_the_repository(
    tmp_path: Path, fake: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[str] = []

    def preference(key: str) -> str | None:
        saved.append(key)
        return "octocat"

    monkeypatch.setattr(github._config, "github_account_preference", preference)
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")

    github_search.branch_pull_requests(root, branch="feature/login")

    assert saved == [f"github.com/{REPOSITORY}"]
    assert fake.calls[0] == ["auth", "token", "--user", "octocat", "-h", "github.com"]
    assert fake.envs[1] is not None and fake.envs[1]["GH_TOKEN"] == TOKEN


def test_with_no_saved_account_the_list_runs_as_ghs_active_one(
    tmp_path: Path, fake: FakeGh
) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")

    github_search.branch_pull_requests(root, branch="feature/login")

    assert [call[:2] for call in fake.calls] == [["pr", "list"]]
    assert fake.envs == [None]


@pytest.mark.parametrize("failure", ["exit", "not-json"])
def test_a_failed_list_names_nothing_gh_said(tmp_path: Path, fake: FakeGh, failure: str) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")
    if failure == "exit":
        fake.list_exit_code = 1
    else:
        fake.raw_output = "<html>example-org/secret-repo</html>"

    with pytest.raises(PullRequestError) as failed:
        github_search.branch_pull_requests(root, branch="feature/login")

    assert str(failed.value) == "GitHub couldn't list this branch's pull requests. Try again."
