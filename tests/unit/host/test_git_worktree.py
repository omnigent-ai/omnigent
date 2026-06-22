"""Tests for host-side git worktree operations.

Exercises ``omnigent.host.git_worktree`` against real ``git`` in a
temp repository — the operations run actual ``git worktree add`` /
``remove`` / ``branch -D`` so a regression in argv construction, repo-
root resolution, or removal ordering fails loud here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.host.git_worktree import (
    CreatedWorktree,
    WorktreeError,
    create_worktree,
    remove_worktree,
    validate_branch_name,
)

# Deterministic identity + config so the tests don't depend on the
# developer's global git config (user.name / init.defaultBranch).
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    """Run a git command in ``repo``, raising on failure.

    :param repo: Repository directory to run in.
    :param args: Git arguments after ``git``, e.g. ``("add", ".")``.
    """
    import os

    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
    )


def _current_branch(path: Path) -> str:
    """Return the checked-out branch name at ``path``.

    :param path: A work tree (main or linked worktree) directory.
    :returns: Branch name, e.g. ``"feature/login"``.
    """
    import os

    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()


def _rev_parse(path: Path, ref: str = "HEAD") -> str:
    """Return the commit sha that ``ref`` resolves to at ``path``.

    :param path: A work tree directory.
    :param ref: Ref to resolve, e.g. ``"HEAD"`` or ``"develop"``.
    :returns: The 40-char commit sha.
    """
    import os

    return subprocess.run(
        ["git", "rev-parse", ref],
        cwd=path,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    """Return whether ``branch`` exists in ``repo``.

    :param repo: Repository directory.
    :param branch: Branch name to check, e.g. ``"feature/login"``.
    :returns: ``True`` if the local branch exists.
    """
    import os

    out = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out != ""


def _worktree_count(repo: Path) -> int:
    """Return how many worktrees are registered for ``repo``.

    :param repo: Repository directory.
    :returns: Worktree count, where ``1`` means only the main work
        tree exists (no linked worktree was added).
    """
    import os

    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout
    # --porcelain emits one "worktree <path>" line per worktree.
    return out.count("worktree ")


@pytest.fixture()
def git_repo(tmp_path: Path) -> Iterator[Path]:
    """Create a one-commit git repo and yield its resolved root.

    :returns: Iterator yielding the repo root path (realpath, so it
        matches what ``git rev-parse --show-toplevel`` returns).
    """
    # Resolve so comparisons match git's realpath output (macOS
    # /tmp -> /private/tmp).
    repo = (tmp_path / "myrepo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    yield repo


def test_create_worktree_places_sibling_of_repo_root(git_repo: Path) -> None:
    """A new worktree lands at ``<repo>-worktrees/<branch>`` with the branch checked out."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    expected = git_repo.parent / "myrepo-worktrees" / "feature-login"
    # Path proves the sibling layout + slash->dash dir sanitization;
    # a regression in _resolve_worktree_path would change this.
    assert created.worktree_path == str(expected)
    assert Path(created.worktree_path).is_dir()
    # The branch is actually checked out in the worktree (not just the dir made).
    assert _current_branch(Path(created.worktree_path)) == "feature/login"
    assert isinstance(created, CreatedWorktree)


def test_create_worktree_resolves_repo_root_from_subdir(git_repo: Path) -> None:
    """Picking a subdir still anchors the worktree at the repo root's sibling."""
    sub = git_repo / "src"
    sub.mkdir()
    created = create_worktree(repo_path=str(sub), branch_name="wip")
    # Sibling of the repo ROOT, not of the picked subdir — proves
    # rev-parse --show-toplevel is used rather than the raw repo_path.
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")


def test_create_worktree_from_linked_worktree_anchors_at_main_repo(git_repo: Path) -> None:
    """Creating a worktree while inside a LINKED worktree anchors at the MAIN repo.

    Resolving the repo root naively (``rev-parse --show-toplevel``) from a
    linked worktree would nest the new worktree under it
    (``…/feature-a-worktrees/feature-b``). ``_main_work_tree`` resolves to
    the main checkout so worktrees stay siblings
    (``…/myrepo-worktrees/feature-b``) — the fork-resume picker prefills a
    worktree as the source session's workspace, so this is the common path.
    """
    # First worktree, created off the main repo.
    first = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    first_path = Path(first.worktree_path)
    assert first_path == git_repo.parent / "myrepo-worktrees" / "feature-a"

    # Second worktree, requested from INSIDE the first (linked) worktree.
    second = create_worktree(repo_path=str(first_path), branch_name="feature/b")

    # Sibling of the MAIN repo, NOT nested under the first worktree. A
    # regression to --show-toplevel would put it under
    # ``feature-a-worktrees/`` and this fails.
    assert second.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "feature-b")
    assert "feature-a-worktrees" not in second.worktree_path
    assert Path(second.worktree_path).is_dir()
    assert _current_branch(Path(second.worktree_path)) == "feature/b"


def test_create_worktree_from_base_branch(git_repo: Path) -> None:
    """A worktree branches from the explicit base ref's tip, not HEAD."""
    # Advance develop with its own commit so it differs from main —
    # otherwise the test would pass even if base_branch were ignored
    # (both would resolve to the same single commit).
    _git(git_repo, "checkout", "-q", "-b", "develop")
    (git_repo / "dev.txt").write_text("dev-only")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "dev commit")
    _git(git_repo, "checkout", "-q", "main")

    created = create_worktree(
        repo_path=str(git_repo), branch_name="from-develop", base_branch="develop"
    )
    assert _current_branch(Path(created.worktree_path)) == "from-develop"
    # Points at develop's tip, not main's — proves base_branch routed
    # the new branch to develop rather than falling back to HEAD.
    assert _rev_parse(Path(created.worktree_path)) == _rev_parse(git_repo, "develop")
    assert _rev_parse(Path(created.worktree_path)) != _rev_parse(git_repo, "main")


def test_create_worktree_unknown_base_branch_fails(git_repo: Path) -> None:
    """An unresolvable base ref fails loud (after the best-effort fetch)."""
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="x", base_branch="nope-not-a-branch")
    # Proves _ensure_base_resolvable rejects rather than silently
    # branching from HEAD when the requested base is missing.
    assert "base branch does not exist" in exc.value.message


@pytest.mark.parametrize("option_like", ["-f", "--exec-path"])
def test_create_worktree_option_like_base_branch_not_executed(
    git_repo: Path, option_like: str
) -> None:
    """A base_branch that looks like a git flag is rejected, never executed.

    ``base_branch`` is user-supplied and reaches ``git rev-parse`` and
    ``git worktree add`` argv. An option-like value (e.g. ``"-f"``, which
    is ``git worktree add``'s ``--force``) must be treated as an
    unresolvable rev, not parsed as a flag. This guards the end-to-end
    security property at the public API: the ref-resolution pre-check and
    the ``--end-of-options`` argv terminators together keep such a value
    from creating a worktree. A regression that let ``"-f"`` through as a
    flag would build a worktree from the wrong base (and force-create it)
    instead of failing — so the assertion below would see a linked
    worktree appear.
    """
    with pytest.raises(WorktreeError):
        create_worktree(repo_path=str(git_repo), branch_name="from-flag", base_branch=option_like)
    # Still only the main work tree — no linked worktree was added, proving
    # git treated the value as a (rejected) rev rather than a flag that
    # would have run `worktree add`. If `-f` were parsed as --force, the
    # count would be 2.
    assert _worktree_count(git_repo) == 1


def test_create_worktree_duplicate_branch_fails(git_repo: Path) -> None:
    """Creating two worktrees for the same branch name fails loud with the friendly error."""
    create_worktree(repo_path=str(git_repo), branch_name="dup")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="dup")
    # The pre-check catches the existing branch before git's raw error;
    # we must NOT silently reuse the existing worktree.
    assert "already exists" in exc.value.message


def test_create_worktree_existing_branch_no_worktree_fails(git_repo: Path) -> None:
    """A branch that exists WITHOUT a worktree is still rejected by the pre-check.

    Proves the pre-check keys off branch existence, not directory
    occupancy — creating a worktree for a plain pre-existing branch
    would otherwise hit git's raw error.
    """
    _git(git_repo, "branch", "preexisting")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="preexisting")
    assert "already exists" in exc.value.message
    assert "preexisting" in exc.value.message


def test_create_worktree_non_repo_fails(tmp_path: Path) -> None:
    """A directory that isn't a git repo is rejected."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(plain), branch_name="x")
    assert "not a git repository" in exc.value.message


def test_remove_worktree_deletes_dir_and_branch(git_repo: Path) -> None:
    """``delete_branch=True`` removes the directory AND the branch."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    remove_worktree(
        worktree_path=created.worktree_path, branch="feature/login", delete_branch=True
    )
    # Directory gone (git worktree remove --force ran)...
    assert not Path(created.worktree_path).exists()
    # ...and the branch deleted (git branch -D ran, after the worktree
    # was removed — git would refuse otherwise).
    assert not _branch_exists(git_repo, "feature/login")


def test_remove_worktree_keeps_branch_when_flag_false(git_repo: Path) -> None:
    """``delete_branch=False`` removes the directory but keeps the branch."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/keep")
    remove_worktree(
        worktree_path=created.worktree_path, branch="feature/keep", delete_branch=False
    )
    assert not Path(created.worktree_path).exists()
    # Branch survives — only the checkout directory was removed.
    assert _branch_exists(git_repo, "feature/keep")


def test_remove_worktree_missing_path_fails(git_repo: Path) -> None:
    """Removing a non-existent worktree path fails loud."""
    with pytest.raises(WorktreeError) as exc:
        remove_worktree(
            worktree_path=str(git_repo.parent / "myrepo-worktrees" / "ghost"),
            branch=None,
            delete_branch=False,
        )
    assert "does not exist" in exc.value.message


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-leading",
        "a..b",
        "a/.hidden",
        "x.lock",
        "x.lock/y",
        "a b",
        "a~b",
        "a:b",
        "/lead",
        "trail/",
    ],
)
def test_validate_branch_name_rejects_bad(bad: str) -> None:
    """Branch names violating git ref-format are rejected before reaching argv."""
    with pytest.raises(WorktreeError):
        validate_branch_name(bad)


@pytest.mark.parametrize("good", ["feature/login", "fix-123", "a/b/c", "release_2", "v1.2"])
def test_validate_branch_name_accepts_good(good: str) -> None:
    """Well-formed branch names pass validation."""
    validate_branch_name(good)  # must not raise
