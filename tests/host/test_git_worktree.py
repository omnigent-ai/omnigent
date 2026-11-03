"""Tests for host-side git worktree operations.

Exercises ``omnigent.host.git_worktree`` against real ``git`` in a
temp repository — the operations run actual ``git worktree add`` /
``remove`` / ``branch -D`` so a regression in argv construction, repo-
root resolution, or removal ordering fails loud here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import omnigent.host.git_worktree as git_worktree_module
from omnigent.host.git_worktree import (
    CreatedWorktree,
    WorktreeError,
    create_worktree,
    list_worktrees,
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
    assert created.workspace == str(expected)
    assert Path(created.worktree_path).is_dir()
    # The branch is actually checked out in the worktree (not just the dir made).
    assert _current_branch(Path(created.worktree_path)) == "feature/login"
    assert isinstance(created, CreatedWorktree)


@pytest.mark.parametrize("linked", [False, True])
def test_create_worktree_resolves_repo_root_from_subdir(git_repo: Path, linked: bool) -> None:
    """Preserve a nested workspace from either the main checkout or a linked worktree."""
    relative = Path("packages") / "my app"
    sub = git_repo / relative
    sub.mkdir(parents=True)
    (sub / "README.md").write_text("nested project")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-qm", "add nested project")
    if linked:
        first = create_worktree(repo_path=str(git_repo), branch_name="first")
        sub = Path(first.worktree_path) / relative
    created = create_worktree(repo_path=str(sub), branch_name="wip")
    # Worktree placement stays anchored at the main repository.
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")
    assert created.workspace == str(Path(created.worktree_path) / relative)
    assert (Path(created.workspace) / "README.md").read_text() == "nested project"
    assert _current_branch(Path(created.workspace)) == "wip"

    with pytest.raises(WorktreeError, match="expected worktree root"):
        remove_worktree(worktree_path=created.workspace, branch="wip", delete_branch=True)
    remove_worktree(worktree_path=created.worktree_path, branch="wip", delete_branch=True)
    assert not Path(created.worktree_path).exists()
    assert not _branch_exists(git_repo, "wip")


@pytest.mark.parametrize("existing_branch", [False, True])
@pytest.mark.parametrize("base_path_kind", ["missing", "file"])
def test_create_worktree_missing_subdir_leaves_no_worktree(
    git_repo: Path, existing_branch: bool, base_path_kind: str
) -> None:
    """A directory missing from the target revision fails before creating a worktree."""
    sub = git_repo / "new-project"
    if base_path_kind == "file":
        sub.write_text("a file in the old revision")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-qm", "add file")
    _git(git_repo, "branch", "old-base")
    if base_path_kind == "file":
        sub.unlink()
    sub.mkdir()
    (sub / "README.md").write_text("new project")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-qm", "add project")
    message = "does not exist" if base_path_kind == "missing" else "is not a directory"
    with pytest.raises(WorktreeError, match=message):
        create_worktree(
            repo_path=str(sub),
            branch_name="old-base" if existing_branch else "new-worktree",
            base_branch=None if existing_branch else "old-base",
            existing_branch=existing_branch,
        )
    assert _worktree_count(git_repo) == 1
    assert not _branch_exists(git_repo, "new-worktree")
    assert _branch_exists(git_repo, "old-base")


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


def test_create_worktree_existing_branch_recreates_after_dir_deleted(git_repo: Path) -> None:
    """A branch whose worktree directory was deleted can be checked back out.

    Simulates the deleted-worktree fork: the directory is rm'd from disk
    (leaving a stale registration), then ``existing_branch=True`` prunes
    the stale entry and adds a fresh worktree for the same branch.
    """
    import shutil

    created = create_worktree(repo_path=str(git_repo), branch_name="fix-1")
    shutil.rmtree(created.worktree_path)
    recreated = create_worktree(repo_path=str(git_repo), branch_name="fix-1", existing_branch=True)
    assert recreated.branch == "fix-1"
    assert Path(recreated.worktree_path).is_dir()
    assert _current_branch(Path(recreated.worktree_path)) == "fix-1"


def test_create_worktree_existing_branch_missing_branch_fails(git_repo: Path) -> None:
    """``existing_branch=True`` for a branch that doesn't exist fails loud."""
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="ghost", existing_branch=True)
    assert "does not exist" in exc.value.message
    assert _worktree_count(git_repo) == 1


def test_create_worktree_existing_branch_live_worktree_fails(git_repo: Path) -> None:
    """``existing_branch=True`` refuses a branch checked out in a LIVE worktree.

    Two sessions must never share one working tree; only a stale (deleted-
    from-disk) registration is pruned, a live one aborts.
    """
    create_worktree(repo_path=str(git_repo), branch_name="busy")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="busy", existing_branch=True)
    assert "already checked out" in exc.value.message
    assert _worktree_count(git_repo) == 2


def test_create_worktree_existing_branch_rejects_base_branch(git_repo: Path) -> None:
    """``existing_branch`` + ``base_branch`` is contradictory and rejected."""
    _git(git_repo, "branch", "have")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(
            repo_path=str(git_repo),
            branch_name="have",
            base_branch="main",
            existing_branch=True,
        )
    assert "base_branch" in exc.value.message


def test_create_worktree_checkout_outlasting_command_timeout_completes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large repo's slow checkout finishes instead of hitting the per-command git timeout."""
    _commit_slow_smudge_file(git_repo, delay_s=3)
    monkeypatch.setattr(git_worktree_module, "_GIT_TIMEOUT_S", 1.0)

    created = create_worktree(repo_path=str(git_repo), branch_name="large-checkout")

    assert (Path(created.worktree_path) / "slow.bin").read_text() == "payload\n"
    assert _current_branch(Path(created.worktree_path)) == "large-checkout"


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
    assert isinstance(main.updated_at, int)


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/omnigent-ai/omnigent.git",
        "https://gitlab.com/acme/repo.git",
        "git@github-personal:omnigent-ai/omnigent.git",
        "ssh://git@github.enterprise.example/omnigent-ai/omnigent.git",
        "https://[invalid/repo",
    ],
)
def test_list_worktrees_does_not_depend_on_remote_url(git_repo: Path, remote_url: str) -> None:
    """Local worktree support is independent of the configured remote."""
    _git(git_repo, "remote", "add", "origin", remote_url)
    result = list_worktrees(repo_path=str(git_repo))
    assert len(result) == 1
    assert result[0].path == str(git_repo)
    assert result[0].branch == "main"
    assert result[0].is_main is True


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
    assert isinstance(linked.updated_at, int)


def test_list_worktrees_fetches_all_timestamps_with_one_git_command(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timestamp metadata stays O(1) as the number of worktrees grows."""
    for index in range(4):
        create_worktree(repo_path=str(git_repo), branch_name=f"feature/{index}")

    original_run_git = git_worktree_module._run_git
    show_calls: list[list[str]] = []

    def run_git(args: list[str], *, cwd: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["show", "-s", "--format=%H%x00%ct"]:
            show_calls.append(args)
        return original_run_git(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(git_worktree_module, "_run_git", run_git)

    result = list_worktrees(repo_path=str(git_repo))

    assert len(result) == 5
    assert len(show_calls) == 1
    assert len(show_calls[0][3:]) == len({_rev_parse(Path(worktree.path)) for worktree in result})
    assert all(isinstance(worktree.updated_at, int) for worktree in result)


def test_list_worktrees_from_linked_resolves_same_list(git_repo: Path) -> None:
    """Listing from inside a linked worktree resolves the main repo's full list."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    nested = Path(created.worktree_path) / "nested"
    nested.mkdir()
    # A subdirectory of the linked worktree resolves both checkouts.
    result = list_worktrees(repo_path=str(nested))
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
    with pytest.raises(WorktreeError) as exc:
        list_worktrees(repo_path=str(plain))
    assert exc.value.message == f"not a git repository: {plain}"


def test_list_worktrees_preserves_invalid_config_error(git_repo: Path) -> None:
    """A broken config is not evidence that an existing repository is non-Git."""
    (git_repo / ".git" / "config").write_text("[broken\n")
    with pytest.raises(WorktreeError, match=r"git worktree list failed.*bad config line"):
        list_worktrees(repo_path=str(git_repo))


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: cannot access '.git/config': Permission denied",
        "fatal: detected dubious ownership in repository at '/repo'",
        "",
    ],
)
def test_list_worktrees_preserves_probe_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    """Unknown probe failures retain diagnostics instead of claiming non-Git."""
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    failed = subprocess.CompletedProcess(
        args=["git", "worktree", "list", "--porcelain"],
        returncode=128,
        stdout="",
        stderr=stderr,
    )
    with patch("omnigent.host.git_worktree.subprocess.run", return_value=failed) as run:
        with pytest.raises(WorktreeError) as exc:
            list_worktrees(repo_path=str(git_repo))
    suffix = f": {stderr}" if stderr else ""
    assert exc.value.message == f"git worktree list failed (exit 128){suffix}"
    assert run.call_args.kwargs["env"]["LC_ALL"] == "C"
    # Git must never wait on a credential prompt the server cannot show.
    assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


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


@pytest.mark.parametrize("branch_has_directory", [True, False])
def test_existing_branch_directory_probe_ignores_same_named_tag(
    git_repo: Path, branch_has_directory: bool
) -> None:
    """Directory validation must inspect the branch that worktree add checks out."""
    before = _rev_parse(git_repo)
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "add web")
    after = _rev_parse(git_repo)
    _git(git_repo, "branch", "feature", after if branch_has_directory else before)
    _git(git_repo, "tag", "feature", before if branch_has_directory else after)
    if branch_has_directory:
        created = create_worktree(
            repo_path=str(source), branch_name="feature", existing_branch=True
        )
        assert Path(created.workspace).is_dir()
        assert (
            next(
                tree.branch
                for tree in list_worktrees(repo_path=created.worktree_path)
                if tree.path == created.worktree_path
            )
            == "feature"
        )
    else:
        with pytest.raises(WorktreeError, match="does not exist"):
            create_worktree(repo_path=str(source), branch_name="feature", existing_branch=True)


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_remove_worktree_rejects_replaced_root(git_repo: Path, replacement: str) -> None:
    """A stale cleanup path must not remove a symlink target or raise an unhandled OS error."""
    original = create_worktree(repo_path=str(git_repo), branch_name="original")
    target = create_worktree(repo_path=str(git_repo), branch_name="target")
    remove_worktree(worktree_path=original.worktree_path)
    path = Path(original.worktree_path)
    if replacement == "file":
        path.write_text("replacement")
    else:
        path.symlink_to(target.worktree_path, target_is_directory=True)
    with pytest.raises(WorktreeError):
        remove_worktree(worktree_path=str(path), branch="original", delete_branch=True)
    assert Path(target.worktree_path).is_dir()
    assert _branch_exists(git_repo, "original")
    assert _branch_exists(git_repo, "target")


def test_worktree_picker_accepts_symlinked_prefix(git_repo: Path) -> None:
    """Picker paths may include a legitimate symlink such as macOS /tmp."""
    alias = git_repo.parent / "alias"
    alias.symlink_to(git_repo.parent, target_is_directory=True)
    created = create_worktree(repo_path=str(git_repo), branch_name="feature")
    trees = list_worktrees(repo_path=str(alias / git_repo.name))
    assert [tree.path for tree in trees] == [str(git_repo), created.worktree_path]


@pytest.mark.parametrize("mode", ["base", "head", "existing"])
def test_directory_validation_survives_revision_moving(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Ref movement cannot make creation succeed with a missing session directory."""
    before = _rev_parse(git_repo)
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "web")
    validated = _rev_parse(git_repo)
    _git(git_repo, "branch", "moving")
    real_run = git_worktree_module._run_git
    moved = False

    def move_after_validation(
        args: list[str], *, cwd: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal moved
        result = real_run(args, cwd=cwd, **kwargs)
        if args[:2] == ["cat-file", "-t"] and not moved:
            ref = "main" if mode == "head" else "moving"
            _git(git_repo, "update-ref", f"refs/heads/{ref}", before)
            moved = True
        return result

    monkeypatch.setattr(git_worktree_module, "_run_git", move_after_validation)
    if mode == "existing":
        with pytest.raises(WorktreeError, match="changed during worktree creation"):
            create_worktree(repo_path=str(source), branch_name="moving", existing_branch=True)
        assert len(list_worktrees(repo_path=str(git_repo))) == 1
        assert _rev_parse(git_repo, "refs/heads/moving") == before
    else:
        created = create_worktree(
            repo_path=str(source),
            branch_name="new",
            base_branch="moving" if mode == "base" else None,
        )
        assert Path(created.workspace).is_dir()
        assert _rev_parse(Path(created.worktree_path)) == validated
    assert moved


@pytest.mark.parametrize("auto_track", ["true", "false", "always", "simple", "inherit"])
def test_subdirectory_creation_preserves_remote_tracking(git_repo: Path, auto_track: str) -> None:
    """Pinned checkouts retain Git's native tracking policy for the requested start ref."""
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "web")
    _git(git_repo, "remote", "add", "origin", str(git_repo))
    _git(git_repo, "fetch", "origin")
    _git(git_repo, "config", "branch.autoSetupMerge", auto_track)
    root_created = create_worktree(
        repo_path=str(git_repo), branch_name="root-pick", base_branch="origin/main"
    )
    nested_created = create_worktree(
        repo_path=str(source), branch_name="nested-pick", base_branch="origin/main"
    )
    root_upstream = _rev_parse(Path(root_created.worktree_path), "@{upstream}")
    nested_upstream = _rev_parse(Path(nested_created.worktree_path), "@{upstream}")
    assert nested_upstream == root_upstream
    assert Path(nested_created.workspace).is_dir()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failed_pinned_checkout_rolls_back_without_hiding_original_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, rollback_fails: bool
) -> None:
    """Checkout errors remain visible even when rollback also fails."""
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "web")
    real_run = git_worktree_module._run_git

    def fail_checkout(
        args: list[str], *, cwd: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["checkout", "--force"]:
            return subprocess.CompletedProcess(args, 1, "", "checkout failed")
        if rollback_fails and args[:2] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(args, 1, "", "rollback failed")
        return real_run(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(git_worktree_module, "_run_git", fail_checkout)
    with pytest.raises(WorktreeError, match="could not check out validated"):
        create_worktree(repo_path=str(source), branch_name="new")
    assert _branch_exists(git_repo, "new") is rollback_fails
    assert len(list_worktrees(repo_path=str(git_repo))) == (2 if rollback_fails else 1)


def _commit_slow_smudge_file(repo: Path, delay_s: int) -> None:
    """Commit ``slow.bin`` behind a smudge filter that sleeps on every checkout.

    :param repo: Repository to commit into.
    :param delay_s: Seconds each checkout of the file takes.
    """
    (repo / ".gitattributes").write_text("slow.bin filter=slow\n")
    (repo / "slow.bin").write_text("payload\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "slow file")
    _git(repo, "config", "filter.slow.smudge", f"sh -c 'exec 2>/dev/null; sleep {delay_s}; cat'")
    _git(repo, "config", "filter.slow.clean", "cat")
    _git(repo, "config", "filter.slow.required", "true")


def test_create_worktree_slow_checkout_beyond_budget_times_out(git_repo: Path) -> None:
    """The create budget still bounds a slow checkout, and the error names it."""
    _commit_slow_smudge_file(git_repo, delay_s=3)

    with pytest.raises(WorktreeError, match="worktree creation timed out after 1s"):
        create_worktree(repo_path=str(git_repo), branch_name="feature/slow", timeout_s=1.0)


@dataclass
class _TimedGit:
    """Real git under a fake clock that advances by a set time per command.

    :param elapsed_s: Simulated seconds per command, keyed like ``"fetch"``
        or ``"worktree add"``.
    :param fail: Commands to report as failed instead of running.
    :param time_out: Commands that use their whole timeout and are killed.
    :param hang_after: Every command after the first one with this key also
        uses its whole timeout and is killed, e.g. ``"fetch"``.
    :param bounds: ``(command, timeout)`` each git process was given, in order.
    :param windows: ``(command, start, latest end)`` on the fake clock for
        each git process, where the latest end is when its timeout kills it.
    """

    elapsed_s: dict[str, float] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)
    time_out: set[str] = field(default_factory=set)
    hang_after: str | None = None
    bounds: list[tuple[str, float]] = field(default_factory=list)
    windows: list[tuple[str, float, float]] = field(default_factory=list)


@pytest.fixture
def timed_git(monkeypatch: pytest.MonkeyPatch) -> _TimedGit:
    """Run git processes against a controllable monotonic clock."""
    timed = _TimedGit()
    clock = [1000.0]
    real_exec = git_worktree_module._exec_git
    monkeypatch.setattr(git_worktree_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def run(argv: list[str], *, cwd: str, timeout: float) -> subprocess.CompletedProcess[str]:
        command = f"worktree {argv[1]}" if argv[0] == "worktree" else argv[0]
        hangs = command in timed.time_out or timed.hang_after in {c for c, _ in timed.bounds}
        timed.bounds.append((command, timeout))
        timed.windows.append((command, clock[0], clock[0] + timeout))
        if hangs:
            clock[0] += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        clock[0] += timed.elapsed_s.get(command, 0.0)
        if command in timed.fail:
            return subprocess.CompletedProcess(argv, 1, "", f"{command} failed")
        return real_exec(argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(git_worktree_module, "_exec_git", run)
    return timed


def _subdirectory_repo_with_unfetched_base(repo: Path) -> Path:
    """Add ``web/`` and an ``origin`` whose ``main`` must be fetched first.

    :returns: The ``web`` directory, so create pins and checks out a commit.
    """
    (repo / "web").mkdir()
    (repo / "web" / "index.html").write_text("<html></html>")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add web")
    _git(repo, "remote", "add", "origin", str(repo))
    return repo / "web"


def test_create_worktree_size_scaling_commands_share_one_budget(
    git_repo: Path, timed_git: _TimedGit
) -> None:
    """Fetch, ``worktree add`` and ``checkout`` draw on one budget; metadata keeps its bound."""
    source = _subdirectory_repo_with_unfetched_base(git_repo)
    timed_git.elapsed_s.update({"fetch": 100.0, "worktree add": 200.0})

    create_worktree(
        repo_path=str(source),
        branch_name="feature/budget",
        base_branch="origin/main",
        timeout_s=1000.0,
    )

    # The pinned checkout's rollback keeps min(120 s, a quarter of the budget) in reserve.
    size_scaling = {"fetch", "worktree add", "checkout"}
    assert [bound for bound in timed_git.bounds if bound[0] in size_scaling] == [
        ("fetch", 880.0),
        ("worktree add", 780.0),
        ("checkout", 580.0),
    ]


@pytest.mark.parametrize(
    ("timeout_s", "fetch_elapsed_s", "hang_after"),
    [(1000.0, 1000.0, None), (1000.0, 870.0, "fetch"), (140.0, 100.0, "fetch")],
    ids=["fetch-spends-budget", "metadata-after-fetch", "legacy-metadata-after-fetch"],
)
def test_create_worktree_stops_once_budget_is_spent(
    git_repo: Path,
    timed_git: _TimedGit,
    timeout_s: float,
    fetch_elapsed_s: float,
    hang_after: str | None,
) -> None:
    """Once fetch, or a metadata command after it, reaches the deadline, the create fails
    before ``worktree add`` starts, without overrunning the budget's reserve."""
    source = _subdirectory_repo_with_unfetched_base(git_repo)
    timed_git.elapsed_s["fetch"] = fetch_elapsed_s
    timed_git.hang_after = hang_after
    reserve = min(git_worktree_module._GIT_TIMEOUT_S, timeout_s / 4)

    with pytest.raises(WorktreeError, match=f"worktree creation timed out after {timeout_s:.0f}s"):
        create_worktree(
            repo_path=str(source),
            branch_name="feature/budget",
            base_branch="origin/main",
            timeout_s=timeout_s,
        )

    assert "worktree add" not in {command for command, _ in timed_git.bounds}
    assert not _branch_exists(git_repo, "feature/budget")
    assert max(end for _, _, end in timed_git.windows) <= 1000.0 + timeout_s
    if hang_after is not None:
        # The stalled metadata command was cut off where the work part of the budget ends.
        assert timed_git.windows[-1][2] == 1000.0 + timeout_s - reserve


@pytest.mark.parametrize(
    ("timeout_s", "checkout_elapsed_s", "checkout_times_out", "expected_remove_bound", "error"),
    [
        (1000.0, 100.0, False, 900.0, "could not check out validated"),
        (1000.0, 880.0, False, 120.0, "could not check out validated"),
        # An older server omits timeout_s and waits 150 s for the reply.
        (140.0, 0.0, True, 35.0, "worktree creation timed out after 140s"),
        (140.0, 100.0, False, 40.0, "could not check out validated"),
    ],
    ids=["budget-left", "reserve-only", "legacy-checkout-timeout", "legacy-late-failure"],
)
def test_create_worktree_rollback_gets_remaining_budget_or_metadata_bound(
    git_repo: Path,
    timed_git: _TimedGit,
    timeout_s: float,
    checkout_elapsed_s: float,
    checkout_times_out: bool,
    expected_remove_bound: float,
    error: str,
) -> None:
    """A failed checkout's rollback uses the rest of the budget, and a reserve kept back
    from fetch, add and checkout lets the whole failure path finish within ``timeout_s``."""
    source = _subdirectory_repo_with_unfetched_base(git_repo)
    timed_git.elapsed_s["checkout"] = checkout_elapsed_s
    if checkout_times_out:
        timed_git.time_out.add("checkout")
    else:
        timed_git.fail.add("checkout")
    deadline = 1000.0 + timeout_s

    with pytest.raises(WorktreeError, match=error):
        create_worktree(repo_path=str(source), branch_name="feature/rollback", timeout_s=timeout_s)

    assert ("worktree remove", expected_remove_bound) in timed_git.bounds
    assert not _branch_exists(git_repo, "feature/rollback")
    # Every git call, rollback included, is killed by the deadline at the latest.
    assert max(end for _, _, end in timed_git.windows) <= deadline
