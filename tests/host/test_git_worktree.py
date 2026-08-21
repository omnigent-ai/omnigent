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
    _resolve_worktree_root,
    create_worktree,
    list_worktrees,
    remove_worktree,
    validate_branch_name,
    validate_worktree_root,
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


def test_list_worktrees_returns_main_first(git_repo: Path) -> None:
    """With no linked worktrees, only the main tree is listed."""
    result = list_worktrees(repo_path=str(git_repo))
    assert len(result) == 1
    main = result[0]
    assert main.path == str(git_repo)
    assert main.branch == "main"
    assert main.is_main is True
    assert main.detached is False


def test_list_worktrees_includes_linked(git_repo: Path) -> None:
    """A created worktree shows up with its branch and is not flagged main."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    result = list_worktrees(repo_path=str(git_repo))
    # Main first, then the linked worktree.
    assert result[0].is_main is True
    linked = next(w for w in result if not w.is_main)
    assert linked.path == created.worktree_path
    assert linked.branch == "feature/login"
    assert linked.detached is False


def test_list_worktrees_from_linked_resolves_same_list(git_repo: Path) -> None:
    """Listing from inside a linked worktree resolves the main repo's full list."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    # Query from the linked worktree — should still see BOTH worktrees.
    result = list_worktrees(repo_path=created.worktree_path)
    paths = {w.path for w in result}
    assert str(git_repo) in paths
    assert created.worktree_path in paths


def test_list_worktrees_reports_detached_head(git_repo: Path) -> None:
    """A detached-HEAD worktree lists with ``branch=None`` and ``detached=True``."""
    head = _rev_parse(git_repo)
    wt = git_repo.parent / "myrepo-worktrees" / "detached"
    wt.parent.mkdir(parents=True, exist_ok=True)
    # Add a worktree checked out at a bare commit → detached HEAD.
    _git(git_repo, "worktree", "add", "--detach", str(wt), head)
    result = list_worktrees(repo_path=str(git_repo))
    detached = next(w for w in result if w.path == str(wt))
    assert detached.branch is None
    assert detached.detached is True


def test_list_worktrees_non_git_path_fails(tmp_path: Path) -> None:
    """A non-git directory fails loud (the route maps this to 'no worktrees')."""
    plain = (tmp_path / "plain").resolve()
    plain.mkdir()
    with pytest.raises(WorktreeError):
        list_worktrees(repo_path=str(plain))


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


# ── Project-configured worktree root ─────────────────────────


def test_configured_root_relative_to_repo_puts_worktree_inside_the_checkout(
    git_repo: Path,
) -> None:
    """A relative root resolves against the repo root, e.g. ``.worktrees``."""
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="feature/login",
        worktree_root=".worktrees",
    )
    assert created.worktree_path == str(git_repo / ".worktrees" / "feature-login")
    assert _current_branch(Path(created.worktree_path)) == "feature/login"


def test_configured_root_can_point_outside_the_repo(git_repo: Path) -> None:
    """``../wt`` is a sibling of the repo — the way to collapse several tools' layouts."""
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="wip",
        worktree_root="../wt",
    )
    assert created.worktree_path == str(git_repo.parent / "wt" / "wip")


def test_configured_root_expands_the_repo_placeholder(git_repo: Path) -> None:
    """``{repo}`` becomes the repo directory name, so one value fits every project."""
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="wip",
        worktree_root="../{repo}-trees",
    )
    assert created.worktree_path == str(git_repo.parent / "myrepo-trees" / "wip")


def test_configured_absolute_root_is_used_verbatim(git_repo: Path, tmp_path: Path) -> None:
    """An absolute root is not re-anchored on the repo."""
    root = tmp_path / "elsewhere"
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="wip",
        worktree_root=str(root),
    )
    assert created.worktree_path == str(root / "wip")


def test_configured_root_is_honored_from_a_linked_worktree(git_repo: Path) -> None:
    """A repo-relative root means the same directory from inside any worktree.

    Otherwise the second worktree would nest under the first, which is the
    scattering this setting exists to stop.
    """
    first = create_worktree(
        repo_path=str(git_repo),
        branch_name="one",
        worktree_root=".worktrees",
    )
    second = create_worktree(
        repo_path=first.worktree_path,
        branch_name="two",
        worktree_root=".worktrees",
    )
    assert second.worktree_path == str(git_repo / ".worktrees" / "two")


def test_configured_root_still_avoids_collisions(git_repo: Path) -> None:
    """Two branches sanitizing to one dir name still get distinct directories.

    ``a/b`` and ``a-b`` are different branches but the same sanitized
    directory, so the numeric suffix has to apply under a configured root
    exactly as it does under the built-in one.
    """
    first = create_worktree(repo_path=str(git_repo), branch_name="a/b", worktree_root=".worktrees")
    second = create_worktree(
        repo_path=str(git_repo), branch_name="a-b", worktree_root=".worktrees"
    )
    assert first.worktree_path == str(git_repo / ".worktrees" / "a-b")
    assert second.worktree_path == str(git_repo / ".worktrees" / "a-b-2")


def test_unset_root_keeps_the_builtin_sibling_layout(git_repo: Path) -> None:
    """``None`` behaves exactly as before the setting existed."""
    created = create_worktree(repo_path=str(git_repo), branch_name="wip", worktree_root=None)
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "{branch}",
        "{repo}/{branch}",
        "wt\x00",
        "x" * 513,
    ],
)
def test_validate_worktree_root_rejects_unusable(bad: str) -> None:
    """A root that would silently produce a nonsense directory is rejected."""
    with pytest.raises(WorktreeError):
        validate_worktree_root(bad)


@pytest.mark.parametrize("same_as_repo", [".", "./", "../myrepo"])
def test_resolving_a_root_onto_the_repo_root_is_refused(same_as_repo: str) -> None:
    """These are valid path syntax but resolve to the checkout itself."""
    with pytest.raises(WorktreeError, match="repository root"):
        _resolve_worktree_root(Path("/home/u/myrepo"), same_as_repo)


@pytest.mark.parametrize(
    "good",
    [".worktrees", "../wt", "{repo}-worktrees", "/abs/wt", "a/b/c", "~/wt"],
)
def test_validate_worktree_root_accepts_reasonable(good: str) -> None:
    """Ordinary layouts, including traversal to a sibling, are allowed."""
    validate_worktree_root(good)  # must not raise


def test_configured_root_rejecting_the_repo_root_itself(git_repo: Path) -> None:
    """``.`` would put checkouts directly in the repo — refused with a hint."""
    with pytest.raises(WorktreeError, match="repository root"):
        create_worktree(repo_path=str(git_repo), branch_name="wip", worktree_root=".")


def test_worktree_can_fork_from_a_branch_checked_out_in_another_worktree(
    git_repo: Path,
) -> None:
    """A fan-out worktree can be based on the orchestrator's live worktree.

    git refuses to CHECK OUT one branch in two worktrees, but using it as a
    start-point for a new branch is fine. This is what lets a fan-out inherit
    the orchestrator's commits instead of forking from the main checkout's
    stale HEAD.
    """
    # The "orchestrator" worktree, with a commit the main checkout lacks.
    orchestrator = create_worktree(repo_path=str(git_repo), branch_name="polly/session")
    orchestrator_path = Path(orchestrator.worktree_path)
    (orchestrator_path / "plan.md").write_text("the plan")
    _git(orchestrator_path, "add", ".")
    _git(orchestrator_path, "commit", "-q", "-m", "orchestrator work")
    orchestrator_head = _rev_parse(orchestrator_path)
    assert orchestrator_head != _rev_parse(git_repo)

    # A worker worktree forked from the orchestrator's branch, requested from
    # INSIDE the orchestrator's worktree (as the tool does).
    worker = create_worktree(
        repo_path=str(orchestrator_path),
        branch_name="polly/task-1",
        base_branch="polly/session",
    )
    worker_path = Path(worker.worktree_path)

    # Same commit, so the orchestrator's work is present in the worker's tree.
    assert _rev_parse(worker_path) == orchestrator_head
    assert (worker_path / "plan.md").read_text() == "the plan"
    # And it is a sibling of the MAIN repo, not nested in the orchestrator's tree.
    assert worker_path == git_repo.parent / "myrepo-worktrees" / "polly-task-1"


def test_worktree_without_a_base_forks_from_the_main_checkout(git_repo: Path) -> None:
    """Documents the behavior the server's default exists to avoid.

    With no base ref, git resolves the start-point from the MAIN work tree —
    so a worktree requested from inside another worktree silently loses that
    worktree's commits. The server therefore defaults the base to the calling
    session's own branch.
    """
    orchestrator = create_worktree(repo_path=str(git_repo), branch_name="polly/session")
    orchestrator_path = Path(orchestrator.worktree_path)
    (orchestrator_path / "plan.md").write_text("the plan")
    _git(orchestrator_path, "add", ".")
    _git(orchestrator_path, "commit", "-q", "-m", "orchestrator work")

    worker = create_worktree(repo_path=str(orchestrator_path), branch_name="unanchored")

    assert _rev_parse(Path(worker.worktree_path)) == _rev_parse(git_repo)
    assert not (Path(worker.worktree_path) / "plan.md").exists()


def test_delete_branch_refused_when_its_work_is_not_merged(git_repo: Path) -> None:
    """An unmerged branch is not deletable, and the worktree survives too.

    This is the guard that keeps a fan-out's output from being destroyed by a
    cleanup step that runs before integration.
    """
    orchestrator = create_worktree(repo_path=str(git_repo), branch_name="polly/session")
    worker = create_worktree(
        repo_path=str(git_repo), branch_name="polly/task-1", base_branch="polly/session"
    )
    worker_path = Path(worker.worktree_path)
    (worker_path / "feature.txt").write_text("the work")
    _git(worker_path, "add", ".")
    _git(worker_path, "commit", "-q", "-m", "task work")

    with pytest.raises(WorktreeError, match="not in 'polly/session' yet"):
        remove_worktree(
            worktree_path=str(worker_path),
            branch="polly/task-1",
            delete_branch=True,
            require_merged_into="polly/session",
        )

    # The refusal is checked BEFORE removal, so nothing was destroyed.
    assert worker_path.is_dir()
    assert _branch_exists(git_repo, "polly/task-1")
    assert Path(orchestrator.worktree_path).is_dir()


def test_delete_branch_allowed_once_its_work_is_merged(git_repo: Path) -> None:
    """After integration the branch is redundant, so cleanup proceeds."""
    orchestrator = create_worktree(repo_path=str(git_repo), branch_name="polly/session")
    orchestrator_path = Path(orchestrator.worktree_path)
    worker = create_worktree(
        repo_path=str(git_repo), branch_name="polly/task-1", base_branch="polly/session"
    )
    worker_path = Path(worker.worktree_path)
    (worker_path / "feature.txt").write_text("the work")
    _git(worker_path, "add", ".")
    _git(worker_path, "commit", "-q", "-m", "task work")

    # The orchestrator integrates the task into its OWN branch.
    _git(orchestrator_path, "merge", "--no-ff", "--no-edit", "polly/task-1")
    assert (orchestrator_path / "feature.txt").read_text() == "the work"

    remove_worktree(
        worktree_path=str(worker_path),
        branch="polly/task-1",
        delete_branch=True,
        require_merged_into="polly/session",
    )
    assert not worker_path.exists()
    assert not _branch_exists(git_repo, "polly/task-1")
    # The integrated work is still in the orchestrator's tree.
    assert (orchestrator_path / "feature.txt").read_text() == "the work"


def test_delete_branch_stays_unconditional_without_a_merge_requirement(
    git_repo: Path,
) -> None:
    """A human deleting a session's worktree is deliberate — no guard.

    Passing no ``require_merged_into`` keeps the pre-existing ``git branch -D``
    behaviour the UI's "delete session and its worktree" flow relies on.
    """
    created = create_worktree(repo_path=str(git_repo), branch_name="scratch")
    worktree_path = Path(created.worktree_path)
    (worktree_path / "wip.txt").write_text("unmerged")
    _git(worktree_path, "add", ".")
    _git(worktree_path, "commit", "-q", "-m", "unmerged work")

    remove_worktree(worktree_path=str(worktree_path), branch="scratch", delete_branch=True)
    assert not worktree_path.exists()
    assert not _branch_exists(git_repo, "scratch")


def test_merge_requirement_ignored_when_the_branch_is_kept(git_repo: Path) -> None:
    """The guard only gates branch DELETION, not worktree removal."""
    created = create_worktree(repo_path=str(git_repo), branch_name="keep-me")
    worktree_path = Path(created.worktree_path)
    (worktree_path / "wip.txt").write_text("unmerged")
    _git(worktree_path, "add", ".")
    _git(worktree_path, "commit", "-q", "-m", "unmerged work")

    remove_worktree(
        worktree_path=str(worktree_path),
        branch="keep-me",
        delete_branch=False,
        require_merged_into="main",
    )
    assert not worktree_path.exists()
    # The branch — and its unmerged commit — survive.
    assert _branch_exists(git_repo, "keep-me")
