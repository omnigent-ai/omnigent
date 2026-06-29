"""Tests for :mod:`omnigent.cursor_cloud_repo`.

Covers URL normalization (SSH / HTTPS / git@ forms, ``.git`` + trailing-slash
stripping), cwd-derived resolution from a real temp git repo's ``origin``
remote and current branch, override precedence, detached-HEAD handling, and the
no-repo error paths. No live cloud calls — repo resolution is pure git + regex.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.cursor_cloud_repo import (
    CursorCloudRepo,
    RepoResolutionError,
    normalize_remote_url,
    resolve_cursor_cloud_repo,
    resolve_cursor_cloud_repos,
)

# ---------------------------------------------------------------------------
# normalize_remote_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # SSH scp-like form.
        ("git@github.com:org/repo.git", "https://github.com/org/repo"),
        ("git@github.com:org/repo", "https://github.com/org/repo"),
        # ssh:// prefixed form.
        ("ssh://git@github.com/org/repo.git", "https://github.com/org/repo"),
        # ssh:// with an explicit port — the port is stripped, not folded into path.
        ("ssh://git@github.com:22/org/repo.git", "https://github.com/org/repo"),
        ("ssh://git@ssh.github.com:443/org/repo", "https://ssh.github.com/org/repo"),
        # HTTPS form with .git suffix.
        ("https://github.com/org/repo.git", "https://github.com/org/repo"),
        ("https://github.com/org/repo", "https://github.com/org/repo"),
        # Trailing slash stripped.
        ("https://github.com/org/repo/", "https://github.com/org/repo"),
        ("git@github.com:org/repo.git/", "https://github.com/org/repo"),
        # Embedded credentials dropped.
        ("https://user:token@github.com/org/repo.git", "https://github.com/org/repo"),
        # git:// scheme normalized to https.
        ("git://github.com/org/repo.git", "https://github.com/org/repo"),
        # Non-GitHub host passed through (still normalized).
        ("git@gitlab.com:group/sub/repo.git", "https://gitlab.com/group/sub/repo"),
        # Whitespace stripped.
        ("  https://github.com/org/repo.git  ", "https://github.com/org/repo"),
    ],
)
def test_normalize_remote_url(raw: str, expected: str) -> None:
    assert normalize_remote_url(raw) == expected


def test_normalize_remote_url_unknown_shape_returned_stripped() -> None:
    # A string matching no known remote shape comes back stripped but unchanged.
    assert normalize_remote_url("  not-a-url  ") == "not-a-url"


# ---------------------------------------------------------------------------
# resolve_cursor_cloud_repo — override precedence
# ---------------------------------------------------------------------------


def test_repo_override_wins_and_is_normalized() -> None:
    resolved = resolve_cursor_cloud_repo(
        None, repo_override="git@github.com:org/repo.git", ref_override="main"
    )
    assert resolved == CursorCloudRepo(url="https://github.com/org/repo", ref="main")


def test_repo_override_without_ref_yields_none_ref() -> None:
    resolved = resolve_cursor_cloud_repo(None, repo_override="https://github.com/org/repo")
    assert resolved.url == "https://github.com/org/repo"
    assert resolved.ref is None


def test_blank_ref_override_folds_to_none() -> None:
    resolved = resolve_cursor_cloud_repo(
        None, repo_override="https://github.com/org/repo", ref_override="   "
    )
    assert resolved.ref is None


# ---------------------------------------------------------------------------
# resolve_cursor_cloud_repo — cwd-derived
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, remote: str | None, branch: str = "feature/x") -> None:
    """Initialize a git repo at *path* on *branch*, optionally with an origin remote."""
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    if remote is not None:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_cwd_derived_url_and_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path, remote="git@github.com:org/repo.git", branch="feature/x")
    resolved = resolve_cursor_cloud_repo(tmp_path)
    assert resolved.url == "https://github.com/org/repo"
    assert resolved.ref == "feature/x"


def test_ref_override_beats_cwd_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path, remote="https://github.com/org/repo.git", branch="feature/x")
    resolved = resolve_cursor_cloud_repo(tmp_path, ref_override="release-1.0")
    assert resolved.url == "https://github.com/org/repo"
    assert resolved.ref == "release-1.0"


def test_detached_head_yields_commit_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path, remote="git@github.com:org/repo.git", branch="main")
    # Detach HEAD at the current commit so `rev-parse --abbrev-ref HEAD` -> "HEAD".
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", sha], check=True)
    resolved = resolve_cursor_cloud_repo(tmp_path)
    assert resolved.url == "https://github.com/org/repo"
    # Detached "HEAD" is not a clonable ref; fall back to the exact commit SHA.
    assert resolved.ref == sha


# ---------------------------------------------------------------------------
# resolve_cursor_cloud_repo — error paths
# ---------------------------------------------------------------------------


def test_no_cwd_and_no_override_raises() -> None:
    with pytest.raises(RepoResolutionError):
        resolve_cursor_cloud_repo(None)


def test_cwd_without_origin_remote_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path, remote=None, branch="main")
    with pytest.raises(RepoResolutionError):
        resolve_cursor_cloud_repo(tmp_path)


def test_non_git_cwd_raises(tmp_path: Path) -> None:
    with pytest.raises(RepoResolutionError):
        resolve_cursor_cloud_repo(tmp_path)


# ---------------------------------------------------------------------------
# resolve_cursor_cloud_repos
# ---------------------------------------------------------------------------


def test_resolve_cursor_cloud_repos_single_url_returns_one_element() -> None:
    result = resolve_cursor_cloud_repos(
        None, repo_override="git@github.com:org/repo.git", ref_override="main"
    )
    assert len(result) == 1
    assert result[0] == CursorCloudRepo(url="https://github.com/org/repo", ref="main")


def test_resolve_cursor_cloud_repos_multiple_urls_all_share_ref() -> None:
    result = resolve_cursor_cloud_repos(
        None,
        repo_override="https://github.com/o/a,git@github.com:o/b.git,https://github.com/o/c",
        ref_override="v2",
    )
    assert len(result) == 3
    assert result[0] == CursorCloudRepo(url="https://github.com/o/a", ref="v2")
    assert result[1] == CursorCloudRepo(url="https://github.com/o/b", ref="v2")
    assert result[2] == CursorCloudRepo(url="https://github.com/o/c", ref="v2")


def test_resolve_cursor_cloud_repos_no_ref_override_yields_none_refs() -> None:
    result = resolve_cursor_cloud_repos(
        None, repo_override="https://github.com/o/a,https://github.com/o/b"
    )
    assert all(r.ref is None for r in result)


def test_resolve_cursor_cloud_repos_no_override_delegates_to_cwd(tmp_path: Path) -> None:
    _init_repo(tmp_path, remote="git@github.com:org/repo.git", branch="feat")
    result = resolve_cursor_cloud_repos(tmp_path)
    assert len(result) == 1
    assert result[0].url == "https://github.com/org/repo"
    assert result[0].ref == "feat"


def test_resolve_cursor_cloud_repos_no_override_no_remote_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path, remote=None, branch="main")
    with pytest.raises(RepoResolutionError):
        resolve_cursor_cloud_repos(tmp_path)
