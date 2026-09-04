"""Tests for :mod:`omnigent.runner.github_resource`.

:func:`github_file_diff` (the on-demand expand-context reader) runs ``git show``
against a real temp repo. The PR-backed :func:`github_changed_files` /
:func:`github_pr_diff` shell out to ``gh``, stubbed here via :func:`_stub_gh`.
:func:`github_info`'s availability fallbacks and its check-summary reducer need
neither ``gh`` nor the network.
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

    def fake_gh(argv: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
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


def _set_pushed_ref(repo: Path, head_ref: str) -> None:
    """Give the ``feature`` branch an upstream whose pushed ref is ``head_ref``.

    Mirrors a fork / triangular push, where the pushed ref (``branch.feature.merge``)
    differs from the local branch name.
    """
    _run(["git", "config", "branch.feature.merge", f"refs/heads/{head_ref}"], repo)


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


def test_github_info_pr_via_pr_list_head(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The PR resolves in one ``gh pr list --head`` call keyed by the pushed ref.

    In a fork / triangular flow the pushed ref (``alice/feature``, from
    ``branch.<n>.merge``) differs from the checkout's ``feature``; ``--head``
    matches on the ref name alone, finding a PR a bare ``gh pr view`` would miss.
    """
    _set_pushed_ref(repo, "alice/feature")
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

    def fake_gh(argv: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "acme/repo"}), "")
        if head == ("pr", "list"):
            return (0, json.dumps([pr]), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"]["number"] == 42
    assert info["pr"]["head_ref"] == "alice/feature"
    assert info["pr"]["is_draft"] is True
    assert info["base_ref"] == "main"
    # One resolve call: gh pr list --head <pushed ref> --state all, no gh pr view.
    list_calls = [c for c in calls if c[:2] == ("pr", "list")]
    assert len(list_calls) == 1
    args = list_calls[0]
    assert args[args.index("--head") + 1] == "alice/feature"
    assert args[args.index("--state") + 1] == "all"
    assert not any(c[:2] == ("pr", "view") for c in calls)


def test_github_info_pr_no_upstream_uses_pr_view(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no upstream to name the head, fall back to a bare ``gh pr view``."""
    # The fixture's `feature` branch has no configured upstream.
    view = {
        "number": 5,
        "title": "t",
        "state": "OPEN",
        "url": "u",
        "isDraft": False,
        "author": {"login": "a"},
        "baseRefName": "main",
        "headRefName": "feature",
        "statusCheckRollup": [],
    }
    calls: list[tuple[str, ...]] = []

    def fake_gh(argv: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
        calls.append(tuple(argv))
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
    assert info["pr"]["number"] == 5
    # No upstream → bare gh pr view, never gh pr list.
    assert any(c[:2] == ("pr", "view") for c in calls)
    assert not any(c[:2] == ("pr", "list") for c in calls)


def test_github_info_pushed_ref_matches_branch_uses_pr_view_not_list(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch pushed under its own name resolves via bare ``gh pr view``, not list.

    Regression: on a base branch like ``master`` the pushed ref equals the local
    name, so a bare ``gh pr view`` (which correctly finds no PR) must be used.
    ``gh pr list --head`` matches the ref name alone and would return a
    stranger's unrelated PR whose head merely shares the name — a false positive.
    """
    _set_pushed_ref(repo, "feature")  # pushed ref == local branch name
    calls: list[tuple[str, ...]] = []

    def fake_gh(argv: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        head = tuple(argv[:2])
        if head == ("auth", "status"):
            return (0, "", "")
        if head == ("repo", "view"):
            return (0, json.dumps({"nameWithOwner": "o/r"}), "")
        if head == ("pr", "view"):
            return (1, "", "no pull requests found for branch")
        if head == ("pr", "list"):
            # A stranger's PR sharing the ref name — must never be consulted here.
            return (0, json.dumps([{"number": 999, "headRefName": "feature"}]), "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")

    info = github_info(str(repo))
    assert info["pr"] is None
    assert info["base_ref"] is None
    assert not any(c[:2] == ("pr", "list") for c in calls)


def test_github_changed_files_via_pr_list_head(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The changed-files list resolves the PR number via ``gh pr list --head`` too.

    Exercises the second call site (:func:`_pr_number`): ``gh pr list --head``
    supplies the number, then the files fetch runs.
    """
    _set_pushed_ref(repo, "alice/feature")
    files = [{"filename": "a.py", "status": "added", "additions": 1, "deletions": 0}]

    def fake_gh(argv: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
        if tuple(argv[:2]) == ("pr", "list"):
            return (0, json.dumps([{"number": 9}]), "")
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
            ("pr", "view"): (0, json.dumps({"number": 7}), ""),
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
            ("pr", "view"): (0, json.dumps({"number": 7}), ""),
            ("pr", "diff"): (0, patch, ""),
        },
    )
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": patch}


def test_github_pr_diff_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no PR for the branch, the patch is empty rather than an error."""
    _stub_gh(monkeypatch, {("pr", "view"): (1, "", "no pull requests found")})
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": ""}


def test_github_pr_diff_via_pr_list_head(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole-PR diff resolves the PR number via ``gh pr list --head``, then diffs it.

    Mirrors the fork / triangular case: a bare ``gh pr diff`` can't resolve the
    head, so the number is resolved by the pushed ref (``branch.<n>.merge``) and
    passed to ``gh pr diff <number>``.
    """
    _set_pushed_ref(repo, "alice/feature")
    patch = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    calls: list[tuple[str, ...]] = []

    def fake_gh(argv: Sequence[str], *, cwd: str) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        head = tuple(argv[:2])
        if head == ("pr", "list"):
            return (0, json.dumps([{"number": 9}]), "")
        if head == ("pr", "diff"):
            return (0, patch, "")
        return (1, "", "no stub")

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    result = github_pr_diff(str(repo))
    assert result == {"object": "session.github.pr_diff", "patch": patch}
    # The diff was fetched by the resolved number, never a bare 'gh pr diff'.
    assert [c for c in calls if c[:2] == ("pr", "diff")] == [("pr", "diff", "9")]


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
