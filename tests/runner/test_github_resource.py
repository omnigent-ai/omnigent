"""Tests for :mod:`omnigent.runner.github_resource`.

:func:`github_file_diff` (the on-demand expand-context reader) runs ``git show``
against a real temp repo. The PR-backed :func:`github_changed_files` /
:func:`github_pr_diff` shell out to ``gh``, stubbed here via :func:`_stub_gh`.
:func:`github_info`'s availability fallbacks, its account enumeration, and its
check-summary reducer need neither ``gh`` nor the network.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnigent.runner import github_resource
from omnigent.runner.github_resource import (
    _summarize_checks,
    github_changed_files,
    github_file_diff,
    github_info,
    github_pr_diff,
)


def _stub_gh(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], tuple[int, str, str]],
) -> None:
    """Stub ``github_resource._gh`` to answer by the argv's leading tokens.

    :param responses: Maps a leading-argv prefix (e.g. ``("pr", "view")``) to
        the ``(returncode, stdout, stderr)`` it should return.
    """

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        for prefix, value in responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return value
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)


def _git_env() -> dict[str, str]:
    """Env with a dummy git identity so commits don't need a configured user."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, env=_git_env())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a ``main`` base and a ``feature`` branch that adds/edits/deletes.

    ``main``: fileA="A base", fileB="B base", fileC="C base".
    ``feature``: fileA→"A changed", fileB deleted, newfile added, fileC untouched.
    """
    _run(["git", "init"], tmp_path)
    (tmp_path / "fileA.py").write_text("A base")
    (tmp_path / "fileB.py").write_text("B base")
    (tmp_path / "fileC.py").write_text("C base")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "base"], tmp_path)
    _run(["git", "branch", "-M", "main"], tmp_path)

    _run(["git", "checkout", "-b", "feature"], tmp_path)
    (tmp_path / "fileA.py").write_text("A changed")
    (tmp_path / "newfile.py").write_text("new content")
    _run(["git", "rm", "fileB.py"], tmp_path)
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "feature"], tmp_path)
    return tmp_path


def test_github_info_gh_not_installed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``gh`` there's no PR knowable, so base/pr/repo are null.

    ``available`` still reflects "is a git repo" and reports the branch; the tab
    is a pure PR view, so ``base_ref`` is null until a PR resolves it.
    """
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: None)
    info = github_info(str(repo))
    assert info["available"] is True
    assert info["gh_available"] is False
    assert info["authenticated"] is False
    assert info["branch"] == "feature"
    assert info["base_ref"] is None
    assert info["pr"] is None
    assert info["repo"] is None


def test_github_info_not_a_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-git workspace reports ``not_a_git_repo`` regardless of ``gh``."""
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")
    info = github_info(str(tmp_path))
    assert info["available"] is False
    assert info["reason"] == "not_a_git_repo"


def test_github_info_pr_via_pr_view(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The PR resolves in one bare ``gh pr view --json`` — fork heads included.

    With the base repo and account pinned, ``gh`` resolves the current branch's
    PR itself (fork / triangular ``alice/feature`` head and all), so there's no
    head-ref heuristic and never a ``gh pr list`` call.
    """
    pr = {
        "number": 42,
        "title": "Add thing",
        "state": "OPEN",
        "url": "https://github.com/acme/repo/pull/42",
        "isDraft": True,
        "author": {"login": "alice"},
        "baseRefName": "main",
        "headRefName": "alice/feature",
        "statusCheckRollup": [],
    }
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        if head == ("pr", "view"):
            return (0, json.dumps(pr), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"]["number"] == 42
    assert info["pr"]["head_ref"] == "alice/feature"
    assert info["pr"]["is_draft"] is True
    assert info["base_ref"] == "main"
    assert any(c[:2] == ("pr", "view") for c in calls)
    assert not any(c[:2] == ("pr", "list") for c in calls)


def test_github_info_no_pr(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch with no PR yields ``pr``/``base_ref`` null (bare ``gh pr view`` empty)."""

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "o/r"}), "")
        if head == ("pr", "view"):
            return (1, "", "no pull requests found for branch")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"] is None
    assert info["base_ref"] is None


def test_github_info_pr_via_commit_fork_fallback(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork PR (``git pp`` to a fork, renamed remote branch): ``gh pr view`` misses
    on the branch name, but the pushed commit resolves the PR from the FORK repo,
    and the PR is then fetched from its own base repo.
    """
    # origin is the fork the branch was pushed to; the PR's base is upstream.
    _run(["git", "remote", "add", "origin", "git@github.com:daniellok-db/repo.git"], repo)
    pr = {
        "number": 77,
        "title": "Renamed head",
        "state": "OPEN",
        "url": "https://github.com/acme/repo/pull/77",
        "isDraft": False,
        "author": {"login": "daniellok-db"},
        "baseRefName": "main",
        "headRefName": "daniellok-db/feature",
        "statusCheckRollup": [],
    }
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            me = {"login": "daniellok-db", "active": True, "state": "success"}
            return (0, json.dumps({"hosts": {"github.com": [me]}}), "")
        if tuple(argv[:2]) == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        # Current-branch lookup misses (gh can't name the renamed fork head).
        if tuple(argv[:2]) == ("pr", "view") and argv[2] == "--json":
            return (1, "", 'no pull requests found for branch "daniellok-db:feature"')
        # The commit lives in the fork, so only the fork's commits/pulls matches.
        if argv[0] == "api" and "repos/daniellok-db/repo/commits/" in argv[1]:
            row = {"number": 77, "state": "open", "base": {"repo": {"full_name": "acme/repo"}}}
            return (0, json.dumps([row]), "")
        # The full object is fetched from the PR's base repo via explicit -R.
        if tuple(argv[:2]) == ("pr", "view") and "-R" in argv:
            assert argv[argv.index("-R") + 1] == "acme/repo"
            assert argv[2] == "77"
            return (0, json.dumps(pr), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"]["number"] == 77
    assert info["pr"]["head_ref"] == "daniellok-db/feature"
    assert info["base_ref"] == "main"
    # Resolved via the FORK's commits/{sha}/pulls, never a base-repo branch list.
    assert any(c[0] == "api" and "daniellok-db/repo/commits/" in c[1] for c in calls)
    # Inference is read-only. The explicit -R lookup above does not need to
    # persist a gh default into the workspace's git config.
    assert not any(c[:2] == ("repo", "set-default") for c in calls)


def test_github_info_no_false_positive_on_default_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On master, the commit fallback must NOT surface the merged PR that
    introduced the tip: gh pr view finds nothing and the fallback returns a
    closed row, so github_info reports no PR."""
    _run(["git", "remote", "add", "origin", "git@github.com:daniellok-db/repo.git"], repo)
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            me = {"login": "daniellok-db", "active": True, "state": "success"}
            return (0, json.dumps({"hosts": {"github.com": [me]}}), "")
        if tuple(argv[:2]) == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        if tuple(argv[:2]) == ("pr", "view"):  # no open PR for master
            return (1, "", 'no pull requests found for branch "daniellok-db:feature"')
        if argv[0] == "api" and "/commits/" in argv[1]:
            # commits/{master-tip}/pulls → the MERGED PR that introduced the tip.
            row = {
                "number": 20050,
                "state": "closed",
                "base": {"ref": "feature", "repo": {"full_name": "acme/repo"}},
            }
            return (0, json.dumps([row]), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"] is None
    assert info["base_ref"] is None
    # It did consult the commit endpoint (so this proves the closed row was
    # rejected, not that the fallback was skipped) but never fetched the PR body.
    assert any(c[0] == "api" and "/commits/" in c[1] for c in calls)
    assert not any(tuple(c[:2]) == ("pr", "view") and "-R" in c for c in calls)


def test_resolve_pr_via_commit_prefers_open(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When one commit maps to several PRs (a stack), the open one is chosen, and
    the PR's own base repo is returned alongside the number."""
    monkeypatch.setattr(github_resource, "_commit_lookup_repo", lambda _root: "acme/repo")

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if argv[0] == "api" and argv[1].endswith("/pulls"):
            rows = [
                {"number": 1, "state": "closed", "base": {"repo": {"full_name": "acme/repo"}}},
                {"number": 2, "state": "open", "base": {"repo": {"full_name": "acme/repo"}}},
            ]
            return (0, json.dumps(rows), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    assert github_resource._resolve_pr_via_commit(str(repo)) == (2, "acme/repo")


def test_resolve_pr_via_commit_rejects_closed_only(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only closed/merged rows → None. On master the endpoint returns the merged
    PR that introduced the tip; that's a false positive, not the branch's PR."""
    monkeypatch.setattr(github_resource, "_commit_lookup_repo", lambda _root: "mlflow/mlflow")

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if argv[0] == "api" and argv[1].endswith("/pulls"):
            # A merged PR whose base is master — what /commits/{master-tip}/pulls returns.
            rows = [
                {
                    "number": 20050,
                    "state": "closed",
                    "base": {"ref": "master", "repo": {"full_name": "mlflow/mlflow"}},
                }
            ]
            return (0, json.dumps(rows), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    assert github_resource._resolve_pr_via_commit(str(repo)) is None


def test_resolve_pr_via_commit_none_without_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No push-target repo (no remote) → returns None without an API call."""
    monkeypatch.setattr(github_resource, "_commit_lookup_repo", lambda _root: None)
    called: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        called.append(tuple(argv))
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    assert github_resource._resolve_pr_via_commit(str(repo)) is None
    assert not any(c[0] == "api" for c in called)


def test_commit_lookup_repo_prefers_tracking_remote(repo: Path) -> None:
    """The single push target is the branch's tracking remote (the fork), not the
    base — and just `origin` when no tracking remote is configured."""
    _run(["git", "remote", "add", "origin", "git@github.com:daniellok-db/repo.git"], repo)
    _run(["git", "remote", "add", "upstream", "git@github.com:acme/repo.git"], repo)
    # No branch.feature.remote yet → falls back to origin.
    assert github_resource._commit_lookup_repo(str(repo)) == "daniellok-db/repo"
    # With a tracking remote set, that wins.
    _run(["git", "config", "branch.feature.remote", "upstream"], repo)
    assert github_resource._commit_lookup_repo(str(repo)) == "acme/repo"


def test_head_commit_shas_includes_head(repo: Path) -> None:
    """HEAD is always a candidate SHA (a fresh branch has no @{push})."""
    import subprocess as _sp

    head = _sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head in github_resource._head_commit_shas(str(repo))


def test_github_info_enumerates_accounts_only_when_repo_unreachable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accounts (for the selector) are enumerated only when the repo can't be
    reached — no PR resolves AND `gh repo view` fails."""
    hosts = {
        "hosts": {
            "github.com": [
                {"login": "alice", "active": True, "state": "success"},
                {"login": "bob", "active": False, "state": "success"},
            ]
        }
    }
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: "/ws/x")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda _key: None)

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            return (0, json.dumps(hosts), "")
        # No PR, and the repo can't be reached → repo-unresolved.
        return (1, "", "no access")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert {a["login"] for a in info["accounts"]} == {"alice", "bob"}
    # No stored preference → the active account is selected. No `remotes` field.
    assert info["selected_account"] == "alice"
    assert "remotes" not in info and "default_remote" not in info


def test_github_info_skips_account_enumeration_on_happy_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a PR resolves, github_info skips `gh auth status` and `gh repo view`
    entirely (deriving the repo from the PR URL)."""
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: "/ws/x")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda _key: None)
    pr = {
        "number": 42,
        "title": "t",
        "state": "OPEN",
        "url": "https://github.com/acme/repo/pull/42",
        "isDraft": False,
        "author": {"login": "a"},
        "baseRefName": "main",
        "headRefName": "feature",
        "statusCheckRollup": [],
    }
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        if tuple(argv[:2]) == ("pr", "view"):
            return (0, json.dumps(pr), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"]["number"] == 42
    assert info["repo"] == {"name_with_owner": "acme/repo"}  # derived from pr.url
    assert info["authenticated"] is True
    # The two per-poll network calls are gone on the happy path.
    assert not any(c[:4] == ("auth", "status", "--json", "hosts") for c in calls)
    assert not any(c[:2] == ("repo", "view") for c in calls)
    assert "accounts" not in info


def test_github_info_runs_gh_as_preferred_account(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace's stored account is applied as GH_TOKEN on the API calls."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: "/ws/omnigent")
    monkeypatch.setattr(
        github_resource._config,
        "github_account_preference",
        lambda key: "bob" if key == "/ws/omnigent" else None,
    )
    monkeypatch.setattr(github_resource, "_gh_auth_token", lambda _root, login: f"tok-{login}")
    seen: dict[str, str | None] = {}

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if tuple(argv[:2]) == ("repo", "view"):
            seen["repo_view"] = token
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        if tuple(argv[:2]) == ("pr", "view"):
            seen["pr_view"] = token
            return (1, "", "no pr")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    # No PR (pr view fails) → falls to the repo-view reachability probe; both run
    # as the preferred account's token.
    github_info(str(repo))
    assert seen["pr_view"] == "tok-bob"
    assert seen["repo_view"] == "tok-bob"


def test_github_changed_files_via_pr_view(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The changed-files list resolves the PR number via bare ``gh pr view``, then fetches."""
    files = [{"filename": "a.py", "status": "added", "additions": 1, "deletions": 0}]

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if tuple(argv[:2]) == ("pr", "view"):
            return (
                0,
                json.dumps({"number": 9, "url": "https://github.com/acme/repo/pull/9"}),
                "",
            )
        if argv and argv[0] == "api":
            return (0, json.dumps(files), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    result = github_changed_files(str(repo))
    assert [entry["path"] for entry in result["data"]] == ["a.py"]


def test_github_changed_files_maps_pr_file_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list comes from ``gh api pulls/<n>/files``, mapping GitHub statuses."""
    files = [
        {"filename": "newfile.py", "status": "added", "additions": 1, "deletions": 0},
        {"filename": "src/fileA.py", "status": "modified", "additions": 2, "deletions": 1},
        {"filename": "fileB.py", "status": "removed", "additions": 0, "deletions": 3},
        {
            "filename": "new/name.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
            "previous_filename": "old/name.py",
        },
    ]
    _stub_gh(
        monkeypatch,
        {
            ("pr", "view"): (
                0,
                json.dumps({"number": 7, "url": "https://github.com/acme/repo/pull/7"}),
                "",
            ),
            ("api",): (0, json.dumps(files), ""),
        },
    )
    result = github_changed_files("/root")
    by_path = {entry["path"]: entry for entry in result["data"]}
    assert by_path["newfile.py"]["status"] == "created"
    assert by_path["src/fileA.py"]["status"] == "modified"
    assert by_path["fileB.py"]["status"] == "deleted"
    assert by_path["new/name.py"]["status"] == "renamed"
    # Line counts and the display name come straight from the PR file entry.
    assert by_path["newfile.py"]["lines_added"] == 1
    assert by_path["src/fileA.py"]["name"] == "fileA.py"


def test_github_file_diff_added(repo: Path) -> None:
    """An added file has no base content but the new HEAD content."""
    diff = github_file_diff(str(repo), "main", "newfile.py")
    assert diff["before"] is None
    assert diff["after"] == "new content"


def test_github_file_diff_modified(repo: Path) -> None:
    """A modified file shows base content as before and HEAD content as after."""
    diff = github_file_diff(str(repo), "main", "fileA.py")
    assert diff["before"] == "A base"
    assert diff["after"] == "A changed"


def test_github_file_diff_deleted(repo: Path) -> None:
    """A deleted file shows base content as before and None as after."""
    diff = github_file_diff(str(repo), "main", "fileB.py")
    assert diff["before"] == "B base"
    assert diff["after"] is None


def test_github_changed_files_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no PR for the branch, the list is empty (no local git fallback)."""
    _stub_gh(monkeypatch, {("pr", "view"): (1, "", "no pull requests found")})
    assert github_changed_files("/root") == {"object": "list", "data": [], "has_more": False}


def test_github_pr_diff_returns_gh_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole-PR patch is ``gh pr diff <number>`` verbatim (GitHub-computed)."""
    patch = "diff --git a/fileA.py b/fileA.py\n@@ -1 +1 @@\n-A base\n+A changed\n"
    _stub_gh(
        monkeypatch,
        {
            ("pr", "view"): (
                0,
                json.dumps({"number": 7, "url": "https://github.com/acme/repo/pull/7"}),
                "",
            ),
            ("pr", "diff"): (0, patch, ""),
        },
    )
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": patch}


def test_github_pr_diff_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no PR for the branch, the patch is empty rather than an error."""
    _stub_gh(monkeypatch, {("pr", "view"): (1, "", "no pull requests found")})
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": ""}


def test_github_pr_diff_resolves_number_then_diffs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-PR diff resolves the number via ``gh pr view``, then ``gh pr diff <n>``."""
    patch = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        head = tuple(argv[:2])
        if head == ("pr", "view"):
            return (
                0,
                json.dumps({"number": 9, "url": "https://github.com/acme/repo/pull/9"}),
                "",
            )
        if head == ("pr", "diff"):
            return (0, patch, "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    result = github_pr_diff(str(repo))
    assert result == {"object": "session.github.pr_diff", "patch": patch}
    # The diff was fetched by the resolved number, never a bare 'gh pr diff'.
    assert [c for c in calls if c[:2] == ("pr", "diff")] == [
        ("pr", "diff", "9", "-R", "github.com/acme/repo")
    ]


@pytest.mark.parametrize("operation", ["changes", "diff"])
def test_github_workspace_operations_keep_fork_pr_base_repo(
    repo: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Commit fallback carries the fork PR's base repo into downstream reads."""
    _run(["git", "remote", "add", "origin", "git@github.com:daniellok-db/repo.git"], repo)
    files = [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1}]
    patch = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        if tuple(argv[:2]) == ("pr", "view") and argv[2] == "--json":
            return (1, "", "no pull requests found for branch")
        if argv[0] == "api" and any("repos/daniellok-db/repo/commits/" in arg for arg in argv):
            row = {"number": 77, "state": "open", "base": {"repo": {"full_name": "acme/repo"}}}
            return (0, json.dumps([row]), "")
        if tuple(argv[:2]) == ("pr", "view") and "-R" in argv:
            assert argv[argv.index("-R") + 1] == "acme/repo"
            return (
                0,
                json.dumps({"number": 77, "url": "https://github.com/acme/repo/pull/77"}),
                "",
            )
        if argv[0] == "api" and any(
            arg == "repos/acme/repo/pulls/77/files?per_page=100" for arg in argv
        ):
            return (0, json.dumps(files), "")
        if tuple(argv[:2]) == ("pr", "diff"):
            return (0, patch, "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)

    if operation == "changes":
        assert [entry["path"] for entry in github_changed_files(str(repo))["data"]] == ["a.py"]
        assert any(
            c[0] == "api" and "repos/acme/repo/pulls/77/files?per_page=100" in c for c in calls
        )
    else:
        assert github_pr_diff(str(repo))["patch"] == patch
        assert ("pr", "diff", "77", "-R", "github.com/acme/repo") in calls
    assert not any(c[:2] == ("repo", "set-default") for c in calls)


# ── Account selection ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/repo.git", "acme/repo"),
        ("https://github.com/acme/repo", "acme/repo"),
        ("https://user@github.com/acme/repo.git", "acme/repo"),
        ("git@github.com:alice/repo.git", "alice/repo"),
        ("ssh://git@github.com/acme/repo.git", "acme/repo"),
        ("not a url", None),
        ("", None),
        (None, None),
    ],
)
def test_owner_repo_from_url(url: str | None, expected: str | None) -> None:
    """Owner/repo parsing handles HTTPS / SSH / scp forms and rejects junk."""
    assert github_resource._owner_repo_from_url(url) == expected


def test_list_accounts_parses_hosts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh auth status --json hosts` yields the accounts and the authed boolean."""
    hosts = {
        "hosts": {
            "github.com": [
                {"login": "alice", "active": True, "state": "success"},
                {"login": "bob", "active": False, "state": "success"},
            ]
        }
    }

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            return (0, json.dumps(hosts), "")
        return (1, "", "")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    authed, accounts = github_resource._list_accounts("/root")
    assert authed is True
    assert [a["login"] for a in accounts] == ["alice", "bob"]
    assert accounts[0]["active"] is True


def test_list_accounts_falls_back_on_old_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old gh without ``--json hosts`` → plain ``gh auth status`` decides the bool."""

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if "--json" in argv:
            return (1, "", "unknown flag: --json")
        if tuple(argv[:2]) == ("auth", "status"):
            return (0, "", "")
        return (1, "", "")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    authed, accounts = github_resource._list_accounts("/root")
    assert authed is True
    assert accounts == []


def test_account_token_for_none_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox keeps its single broker identity — never a per-workspace override."""
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: "/ws/omnigent")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda _key: "bob")
    assert github_resource._account_token_for("/root") is None


def test_account_token_for_none_without_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stored preference → no override (gh's active account is used)."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: "/ws/omnigent")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda _key: None)
    assert github_resource._account_token_for("/root") is None


def test_set_github_preference_sets_default_and_account(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selection runs ``gh repo set-default`` (remote) and stores the account pref
    keyed by the workspace, not the base repo."""
    calls: list[tuple[str, ...]] = []
    saved: dict[str, str | None] = {}
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: "/ws/omnigent")
    monkeypatch.setattr(
        github_resource._config,
        "set_github_account_preference",
        lambda key, login, *a, **k: saved.update({"key": key, "login": login}),
    )
    monkeypatch.setattr(github_resource, "github_info", lambda _root, **_kwargs: {"stub": True})

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        return (0, "", "")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    out = github_resource.set_github_preference(str(repo), account="bob", remote="fork")
    assert ("repo", "set-default", "fork") in calls
    assert saved == {"key": "/ws/omnigent", "login": "bob"}
    assert out == {"stub": True}


def test_workspace_key_shared_across_worktrees(repo: Path) -> None:
    """The key is the main worktree path, so a linked worktree resolves to it too."""
    import os

    key_main = github_resource._workspace_key(str(repo))
    assert key_main is not None
    # Points at the main worktree (realpath-compared: git resolves symlinks).
    assert os.path.realpath(key_main) == os.path.realpath(str(repo))
    # A linked worktree of the same repo yields the SAME key (shared .git).
    wt = repo.parent / f"{repo.name}-wt"
    _run(["git", "worktree", "add", "-b", "wt-feature", str(wt)], repo)
    assert github_resource._workspace_key(str(wt)) == key_main


# ── _gh environment handling ─────────────────────────────────────────────────


def test_summarize_checks_mixed() -> None:
    """The reducer classifies CheckRun (status/conclusion) and StatusContext (state)."""
    rollup = [
        {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u"},
        {"name": "e2e", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"workflowName": "bench", "status": "IN_PROGRESS", "conclusion": None},
        {"context": "legacy-ok", "state": "SUCCESS", "targetUrl": "t"},
        {"context": "legacy-wait", "state": "PENDING"},
        {"context": "legacy-err", "state": "ERROR"},
    ]
    result = _summarize_checks(rollup)
    assert result["passing"] == 2
    assert result["failing"] == 2
    assert result["pending"] == 2
    assert result["total"] == 6
    # Per-check details carry the job name, bucket, and link (name falls back to
    # context / workflowName; url falls back to targetUrl).
    assert {"name": "unit", "bucket": "passing", "url": "u"} in result["runs"]
    assert {"name": "e2e", "bucket": "failing", "url": None} in result["runs"]
    assert {"name": "bench", "bucket": "pending", "url": None} in result["runs"]
    assert {"name": "legacy-ok", "bucket": "passing", "url": "t"} in result["runs"]


def test_summarize_checks_empty() -> None:
    """A missing/empty rollup summarizes to all zeros with no runs."""
    assert _summarize_checks(None) == {
        "passing": 0,
        "failing": 0,
        "pending": 0,
        "total": 0,
        "runs": [],
    }


def test_github_info_includes_body_and_comments(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PR carries its description and its shaped conversation comments.

    ``body`` feeds the Summary tab's description; ``comments`` its list. A
    minimized/collapsed comment is dropped (as GitHub hides it), and each entry
    is flattened to ``{author, body, created_at, url}``.
    """
    view = {
        "number": 8,
        "title": "t",
        "state": "OPEN",
        "url": "u",
        "isDraft": False,
        "author": {"login": "a"},
        "baseRefName": "main",
        "headRefName": "feature",
        "statusCheckRollup": [],
        "body": "## Summary\nDoes things.",
        "comments": [
            {
                "author": {"login": "reviewer"},
                "body": "nice",
                "createdAt": "2026-09-05T07:32:02Z",
                "url": "https://example.com/c/1",
                "isMinimized": False,
            },
            {
                "author": {"login": "spammer"},
                "body": "hidden",
                "createdAt": "2026-09-05T08:00:00Z",
                "url": "https://example.com/c/2",
                "isMinimized": True,
            },
        ],
    }

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "o/r"}), "")
        if head == ("pr", "view"):
            return (0, json.dumps(view), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"]["body"] == "## Summary\nDoes things."
    # The minimized comment is dropped; the survivor is flattened to snake_case.
    assert info["pr"]["comments"] == [
        {
            "author": "reviewer",
            "body": "nice",
            "created_at": "2026-09-05T07:32:02Z",
            "url": "https://example.com/c/1",
        }
    ]


def test_github_info_empty_body_is_null(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank PR body becomes null so the UI shows its 'no description' state."""
    view = {
        "number": 8,
        "title": "t",
        "state": "OPEN",
        "url": "u",
        "isDraft": False,
        "author": {"login": "a"},
        "baseRefName": "main",
        "headRefName": "feature",
        "statusCheckRollup": [],
        "body": "   ",
        "comments": [],
    }

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "o/r"}), "")
        if head == ("pr", "view"):
            return (0, json.dumps(view), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"]["body"] is None
    assert info["pr"]["comments"] == []


def test_shape_comments_filters_minimized_and_caps() -> None:
    """Minimized comments drop out and the list caps at ``_MAX_COMMENTS``."""
    raw: list[dict[str, object]] = [
        {"author": {"login": "min"}, "body": "x", "isMinimized": True},
    ]
    raw += [
        {"author": {"login": f"u{i}"}, "body": f"c{i}", "createdAt": "t", "url": None}
        for i in range(github_resource._MAX_COMMENTS + 5)
    ]
    shaped = github_resource._shape_comments(raw)
    assert len(shaped) == github_resource._MAX_COMMENTS
    assert all(c["author"] != "min" for c in shaped)
    # A missing author flattens to None; a non-list input is an empty list.
    assert github_resource._shape_comments([{"body": "hi"}]) == [
        {"author": None, "body": "hi", "created_at": None, "url": None}
    ]
    assert github_resource._shape_comments(None) == []


def test_gh_scrubs_env_tokens_in_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # In a sandbox the panel's gh must authenticate as the connected owner via
    # hosts.yml, never an ambient GH_TOKEN/GITHUB_TOKEN (gh ranks those above
    # hosts.yml) — so they're scrubbed from gh's env, restoring the fail-closed
    # property and preventing a stray token from making the panel a shared identity.
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv("GH_TOKEN", "shared-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-tok")
    captured: dict[str, object] = {}

    def fake_run(argv: Sequence[str], *, cwd: str, timeout: float, env=None):
        captured["argv"] = list(argv)
        captured["env"] = env
        return 0, "", ""

    monkeypatch.setattr(github_resource, "_run", fake_run)
    github_resource._gh(["api", "user"], cwd="/tmp")
    assert captured["argv"] == ["gh", "api", "user"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_gh_inherits_env_outside_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local dev (not a sandbox): env is inherited untouched (env=None), so the
    # developer's own gh auth / GH_TOKEN keeps working — no regression.
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    captured: dict[str, object] = {}

    def fake_run(argv: Sequence[str], *, cwd: str, timeout: float, env=None):
        captured["env"] = env
        return 0, "", ""

    monkeypatch.setattr(github_resource, "_run", fake_run)
    github_resource._gh(["pr", "diff"], cwd="/tmp")
    assert captured["env"] is None


def test_gh_applies_account_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit token runs this one call as the selected account: GH_TOKEN is
    # set to it and any stray GITHUB_TOKEN is dropped so it can't win.
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "stray")
    captured: dict[str, object] = {}

    def fake_run(argv: Sequence[str], *, cwd: str, timeout: float, env=None):
        captured["env"] = env
        return 0, "", ""

    monkeypatch.setattr(github_resource, "_run", fake_run)
    github_resource._gh(["pr", "view"], cwd="/tmp", token="chosen-tok")
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GH_TOKEN"] == "chosen-tok"
    assert "GITHUB_TOKEN" not in env
