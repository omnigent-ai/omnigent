"""Tests for :mod:`omnigent.runner.github_resource`.

The ``git``-backed functions (:func:`github_changed_files`,
:func:`github_file_diff`) run against a real temp repo so the subprocess path is
fully exercised. The ``gh``-backed :func:`github_info` is covered for its
availability fallbacks (``gh`` missing, not a git repo) and its check-summary
reducer, which don't require ``gh`` or the network.
"""

from __future__ import annotations

import os
import subprocess
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


def test_github_info_gh_not_installed_still_serves_git(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``gh``, a git repo still reports branch + base so the diff renders.

    This is what lets the host fallback serve the branch-vs-base diff when the
    runner is offline and its machine has no ``gh``.
    """
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: None)
    info = github_info(str(repo))
    assert info["available"] is True
    assert info["gh_available"] is False
    assert info["authenticated"] is False
    assert info["branch"] == "feature"
    assert info["base_ref"] == "main"
    assert info["pr"] is None
    assert info["repo"] is None


def test_github_info_not_a_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-git workspace reports ``not_a_git_repo`` regardless of ``gh``."""
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")
    info = github_info(str(tmp_path))
    assert info["available"] is False
    assert info["reason"] == "not_a_git_repo"


def test_github_changed_files_statuses(repo: Path) -> None:
    """Changed-files list reports add/modify/delete against the base branch."""
    result = github_changed_files(str(repo), "main")
    by_path = {entry["path"]: entry for entry in result["data"]}
    assert by_path["newfile.py"]["status"] == "created"
    assert by_path["fileA.py"]["status"] == "modified"
    assert by_path["fileB.py"]["status"] == "deleted"
    assert "fileC.py" not in by_path
    # Line counts come from numstat: the added file gains a line.
    assert by_path["newfile.py"]["lines_added"] == 1
    assert by_path["newfile.py"]["name"] == "newfile.py"


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


def test_github_changed_files_unresolvable_base(repo: Path) -> None:
    """An unknown base ref yields an empty list rather than an error."""
    result = github_changed_files(str(repo), "does-not-exist")
    assert result == {"object": "list", "data": [], "has_more": False}


def test_github_pr_diff_covers_all_files(repo: Path) -> None:
    """The whole-PR patch is one unified diff spanning every changed file."""
    result = github_pr_diff(str(repo), "main")
    patch = result["patch"]
    # One diff header per changed file, plus the actual change content.
    assert "diff --git a/fileA.py b/fileA.py" in patch
    assert "diff --git a/newfile.py b/newfile.py" in patch
    assert "diff --git a/fileB.py b/fileB.py" in patch
    assert "+A changed" in patch
    assert "fileC.py" not in patch  # unchanged file absent


def test_github_pr_diff_unresolvable_base(repo: Path) -> None:
    """An unknown base ref yields an empty patch rather than an error."""
    assert github_pr_diff(str(repo), "does-not-exist") == {
        "object": "session.github.pr_diff",
        "patch": "",
    }


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
