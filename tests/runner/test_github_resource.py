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
    """The whole-PR patch is ``gh pr diff`` verbatim (GitHub-computed)."""
    patch = "diff --git a/fileA.py b/fileA.py\n@@ -1 +1 @@\n-A base\n+A changed\n"
    _stub_gh(monkeypatch, {("pr", "diff"): (0, patch, "")})
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": patch}


def test_github_pr_diff_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no PR for the branch, the patch is empty rather than an error."""
    _stub_gh(monkeypatch, {("pr", "diff"): (1, "", "no pull requests found")})
    assert github_pr_diff("/root") == {"object": "session.github.pr_diff", "patch": ""}


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
