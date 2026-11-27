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


def _entry(
    number: int, head: str = "feature/login", owner: str = "example-org", **fields: Any
) -> dict[str, Any]:
    """A pull request as GitHub's REST pulls list gives it, from ``owner``'s ``head``."""
    return {
        "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "draft": False,
        "updated_at": "2026-01-02T03:04:05Z",
        "merged_at": None,
        "head": {"ref": head, "repo": {"full_name": f"{owner}/example-repo"}},
        **fields,
    }


def _from_fork(number: int, owner: str) -> dict[str, Any]:
    """A pull request into the repository from ``owner``'s fork's ``feature/login``."""
    return _entry(number, owner=owner)


def _list_request(argv: list[str]) -> tuple[str, str, dict[str, str]]:
    """The host, endpoint and fields of a ``gh api --method GET`` call."""
    fields = dict(
        argv[index + 1].split("=", 1) for index, flag in enumerate(argv) if flag in {"-f", "-F"}
    )
    return argv[argv.index("--hostname") + 1], argv[argv.index("GET") + 1], fields


def _github_keeps(entry: Any, fields: dict[str, str]) -> bool:
    """Whether GitHub's ``head=<owner>:<branch>`` filter keeps ``entry``."""
    owner, branch = fields["head"].split(":", 1)
    head = (entry.get("head") or {}) if isinstance(entry, dict) else {}
    full_name = (head.get("repo") or {}).get("full_name", "")
    return head.get("ref") == branch and full_name.split("/")[0].lower() == owner.lower()


class FakeGh:
    """Answers the pulls list from ``listed``, newest first, as GitHub does; git runs for real.

    Like GitHub, it keeps only the requested head's pull requests, then cuts the page at
    ``per_page``. With ``filtered`` off, it answers ``listed`` as it is, as GitHub never would.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.listed: Any = []
        self.filtered = True
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
            assert argv[1] == "api", argv
            if self.list_exit_code:
                return self.list_exit_code, "", "HTTP 502: example-org/secret-repo is unreachable"
            if self.raw_output is not None:
                return 0, self.raw_output, ""
            if not self.filtered:
                return 0, json.dumps(self.listed), ""
            _host, _endpoint, fields = _list_request(argv)
            kept = [entry for entry in self.listed if _github_keeps(entry, fields)]
            return 0, json.dumps(kept[: int(fields["per_page"])]), ""

        monkeypatch.setattr(github, "_run", run)

    def list_request(self) -> tuple[str, str, dict[str, str]]:
        (argv,) = [call for call in self.calls if call[0] == "api"]
        return _list_request(argv)


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


def _commit(root: str) -> None:
    """A first commit, so git can name the checked-out branch."""
    identity = ["-c", "user.name=Example", "-c", "user.email=dev@example.com"]
    commit = ["commit", "-q", "--allow-empty", "-m", "start"]
    subprocess.run(["git", *identity, *commit], cwd=root, check=True)


def _git_config(root: str, key: str, value: str) -> None:
    subprocess.run(["git", "config", key, value], cwd=root, check=True)


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
    fake.listed = [
        _entry(7),
        _entry(5, state="closed", merged_at="2026-01-03T04:05:06Z", draft=True),
        _entry(4, state="closed"),
    ]

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
            {
                "url": f"https://github.com/{REPOSITORY}/pull/4",
                "number": 4,
                "title": "PR 4",
                "state": "CLOSED",
                "is_draft": False,
                "updated_at": "2026-01-02T03:04:05Z",
                "head_ref": "feature/login",
            },
        ],
        "has_more": False,
    }
    # GitHub keeps only this owner's branch before the page is cut, one past the cap.
    assert fake.list_request() == (
        "github.com",
        f"repos/{REPOSITORY}/pulls",
        {"head": "example-org:feature/login", "state": "all", "per_page": "21"},
    )
    assert len(fake.calls) == 1


def test_a_branch_name_is_one_field_never_part_of_the_path(tmp_path: Path, fake: FakeGh) -> None:
    """gh sends each field URL-encoded, so ``&`` or ``#`` can't add a query parameter."""
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")

    github_search.branch_pull_requests(root, branch="feature/a&state=open#x")

    (argv,) = fake.calls
    assert argv[argv.index("GET") + 1] == f"repos/{REPOSITORY}/pulls"
    assert argv[argv.index("head=example-org:feature/a&state=open#x") - 1] == "-f"
    assert fake.list_request()[2]["state"] == "all"


@pytest.mark.parametrize(
    "remote",
    [
        f"ssh://git@github.com:22/{REPOSITORY}.git",
        f"https://github.com:443/{REPOSITORY}",
        f"git@github.com:{REPOSITORY}.git",
    ],
    ids=["ssh-port", "https-port", "scp"],
)
def test_every_remote_form_lists_the_same_repository(
    tmp_path: Path, fake: FakeGh, remote: str
) -> None:
    root = _checkout(tmp_path, origin=remote)
    fake.listed = [_entry(7)]

    rows = github_search.branch_pull_requests(root, branch="feature/login")["data"]

    assert [row["url"] for row in rows] == [f"https://github.com/{REPOSITORY}/pull/7"]
    assert fake.list_request()[:2] == ("github.com", f"repos/{REPOSITORY}/pulls")


def test_the_base_remote_gh_resolved_is_listed_before_tracking_and_origin(
    tmp_path: Path, fake: FakeGh
) -> None:
    """A fork's pull requests live in the base repository ``gh repo set-default`` chose."""
    root = _checkout(
        tmp_path,
        origin="https://github.com/someone/example-repo.git",
        fork="https://github.com/another/example-repo.git",
        upstream=f"https://github.com/{REPOSITORY}.git",
    )
    _commit(root)
    _git_config(root, "branch.main.remote", "fork")
    _git_config(root, "remote.upstream.gh-resolved", "base")

    github_search.branch_pull_requests(root, branch="feature/login")

    assert fake.list_request()[:2] == ("github.com", f"repos/{REPOSITORY}/pulls")


@pytest.mark.parametrize(
    "tracking",
    [
        f"https://github.com/{REPOSITORY}.git",
        f"ssh://git@github.com:22/{REPOSITORY}.git",
        f"https://github.com:443/{REPOSITORY}",
    ],
    ids=["https", "ssh-port", "https-port"],
)
def test_without_a_base_remote_the_branchs_tracking_remote_is_listed(
    tmp_path: Path, fake: FakeGh, tracking: str
) -> None:
    root = _checkout(
        tmp_path, origin="https://github.com/someone/example-repo.git", tracked=tracking
    )
    _commit(root)
    _git_config(root, "branch.main.remote", "tracked")

    github_search.branch_pull_requests(root, branch="feature/login")

    assert fake.list_request()[:2] == ("github.com", f"repos/{REPOSITORY}/pulls")


def _fork_checkout(tmp_path: Path, **settings: str) -> str:
    """A fork workflow: ``origin`` is someone's fork, and gh's base is the repository."""
    root = _checkout(
        tmp_path,
        origin="https://github.com/someone/example-repo.git",
        upstream=f"https://github.com/{REPOSITORY}.git",
        mirror="https://github.com/mirror-owner/example-repo.git",
    )
    _git_config(root, "remote.upstream.gh-resolved", "base")
    for key, value in settings.items():
        _git_config(root, key, value)
    return root


@pytest.mark.parametrize(
    ("settings", "pushed_from"),
    [
        ({}, "someone"),
        ({"branch.feature/login.remote": "mirror"}, "mirror-owner"),
        ({"remote.pushDefault": "mirror"}, "mirror-owner"),
        (
            {"branch.feature/login.remote": "origin", "branch.feature/login.pushRemote": "mirror"},
            "mirror-owner",
        ),
    ],
    ids=["origin", "branch-remote", "push-default", "push-remote"],
)
def test_a_fork_lists_its_own_branchs_pull_request_but_not_another_forks(
    tmp_path: Path, fake: FakeGh, settings: dict[str, str], pushed_from: str
) -> None:
    """Another fork's ``feature/login`` is a different branch, though it has the same name.

    More than a page of newer pull requests from other forks' branches comes first. They
    can't crowd this branch's out, because GitHub keeps only its owner's before the cut.
    """
    root = _fork_checkout(tmp_path, **settings)
    newer = [_from_fork(number, "another") for number in range(100, 79, -1)]
    owners = ["someone", "mirror-owner", "another", "example-org"]
    fake.listed = newer + [_from_fork(number, owner) for number, owner in enumerate(owners, 1)]

    found = github_search.branch_pull_requests(root, branch="feature/login")

    assert fake.list_request()[2]["head"] == f"{pushed_from}:feature/login"
    assert [row["number"] for row in found["data"]] == [owners.index(pushed_from) + 1]
    assert found["has_more"] is False


def test_a_same_repository_branch_lists_no_forks_pull_request(
    tmp_path: Path, fake: FakeGh
) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}.git")
    fake.listed = [_entry(1), _from_fork(2, "another")]

    rows = github_search.branch_pull_requests(root, branch="feature/login")["data"]

    assert [row["number"] for row in rows] == [1]


@pytest.mark.parametrize(
    "listed",
    [
        # Another repository's, another host's, and one that's no pull request URL.
        [_entry(1, html_url="https://github.com/example-org/other-repo/pull/1")],
        [_entry(2, html_url=f"https://{GHES}/{REPOSITORY}/pull/2")],
        [_entry(3, html_url="javascript:alert(1)")],
        # Another branch's, and entries GitHub never sends.
        [_entry(4, head="main")],
        # Another fork's same-named branch, and a head repository since deleted.
        [_from_fork(7, "another")],
        [_entry(8, head={"ref": "feature/login", "repo": None})],
        [_entry(5, html_url=None)],
        ["not an entry"],
        {"html_url": f"https://github.com/{REPOSITORY}/pull/6"},
    ],
    ids=[
        "other-repository",
        "other-host",
        "not-a-url",
        "other-branch",
        "other-fork",
        "deleted-fork",
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
    fake.filtered = False
    fake.listed = listed

    assert github_search.branch_pull_requests(root, branch="feature/login")["data"] == []


def test_a_title_that_isnt_text_is_shown_empty(tmp_path: Path, fake: FakeGh) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")
    fake.listed = [_entry(7, title={"html": "<b>x</b>"})]

    (row,) = github_search.branch_pull_requests(root, branch="feature/login")["data"]

    assert row["title"] == ""


@pytest.mark.parametrize(("listed", "shown", "has_more"), [(21, 20, True), (20, 20, False)])
def test_the_list_is_capped_and_says_when_github_has_more(
    tmp_path: Path, fake: FakeGh, listed: int, shown: int, has_more: bool
) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")
    fake.listed = [_entry(number) for number in range(1, listed + 1)]

    found = github_search.branch_pull_requests(root, branch="feature/login")

    assert len(found["data"]) == shown
    assert found["has_more"] is has_more


@pytest.mark.parametrize(
    "remotes",
    [
        {},
        {"origin": "/srv/mirror/example-repo"},
        {"origin": f"https://github.com/{REPOSITORY}?per_page=100"},
    ],
    ids=["no-remote", "path-remote", "not-a-repository-name"],
)
def test_a_workspace_without_a_repository_remote_has_none(
    tmp_path: Path, fake: FakeGh, remotes: dict[str, str]
) -> None:
    root = _checkout(tmp_path, **remotes)

    assert github_search.branch_pull_requests(root, branch="main") == {
        "object": "list",
        "data": [],
        "has_more": False,
    }
    assert fake.calls == []


@pytest.mark.parametrize(
    "remote",
    [f"https://{GHES}/{REPOSITORY}.git", f"ssh://git@{GHES}:7999/{REPOSITORY}.git"],
    ids=["https", "ssh-port"],
)
def test_a_remote_on_a_host_never_set_up_runs_no_gh_with_an_enterprise_token(
    tmp_path: Path, fake: FakeGh, monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    """gh would treat every enterprise host as signed in and send it this token."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)
    root = _checkout(tmp_path, origin=remote)

    with pytest.raises(PullRequestError, match="isn't on a GitHub host set up here"):
        github_search.branch_pull_requests(root, branch="main")

    assert fake.calls == []


def test_a_remote_on_the_gh_host_set_up_is_listed(
    tmp_path: Path, fake: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    root = _checkout(tmp_path, origin=f"https://{GHES}/{REPOSITORY}.git")
    fake.listed = [_entry(7, html_url=f"https://{GHES}/{REPOSITORY}/pull/7")]

    rows = github_search.branch_pull_requests(root, branch="feature/login")["data"]

    assert [row["url"] for row in rows] == [f"https://{GHES}/{REPOSITORY}/pull/7"]
    assert fake.list_request()[:2] == (GHES, f"repos/{REPOSITORY}/pulls")
    # No sign-in probe: the host was checked without running gh.
    assert [call[0] for call in fake.calls] == ["api"]


@pytest.mark.parametrize(
    "remote_repository", [REPOSITORY, "Example-Org/Example-Repo"], ids=["lower", "mixed-case"]
)
def test_the_list_runs_as_the_account_saved_for_the_repository(
    tmp_path: Path, fake: FakeGh, monkeypatch: pytest.MonkeyPatch, remote_repository: str
) -> None:
    """The account is saved under the key a pull request URL gives, which is lower-cased."""
    saved: list[str] = []

    def preference(key: str) -> str | None:
        saved.append(key)
        return {f"github.com/{REPOSITORY}": "octocat"}.get(key)

    monkeypatch.setattr(github._config, "github_account_preference", preference)
    root = _checkout(tmp_path, origin=f"https://github.com/{remote_repository}")

    github_search.branch_pull_requests(root, branch="feature/login")

    assert saved == [f"github.com/{REPOSITORY}"]
    assert fake.calls[0] == ["auth", "token", "--user", "octocat", "-h", "github.com"]
    assert fake.envs[1] is not None and fake.envs[1]["GH_TOKEN"] == TOKEN


def test_with_no_saved_account_the_list_runs_as_ghs_active_one(
    tmp_path: Path, fake: FakeGh
) -> None:
    root = _checkout(tmp_path, origin=f"https://github.com/{REPOSITORY}")

    github_search.branch_pull_requests(root, branch="feature/login")

    assert [call[0] for call in fake.calls] == ["api"]
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
