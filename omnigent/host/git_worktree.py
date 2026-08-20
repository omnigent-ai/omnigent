"""Host-side git worktree operations for session-start worktrees.

Runs ``git`` (via argv lists, never a shell) on the host in response to
``host.create_worktree`` / ``host.remove_worktree`` frames. Branch names
are validated against git ref-format rules before reaching argv. See
designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# fetch/add can be slow on large repos; bound it so git can't hang the
# host's tunnel loop.
_GIT_TIMEOUT_S: float = 120.0

# Max directory-collision suffixes (``-2`` .. ``-N``) before giving up.
_MAX_DIR_COLLISION_SUFFIX: int = 50

# Chars git refuses in a ref: space, control chars, ``~^:?*[\``, DEL.
# (``..``, leading ``-``/``.``, ``/`` edges, ``.lock``, ``@{`` are
# checked separately.)
_INVALID_BRANCH_CHARS = re.compile(r"[\x00-\x20~^:?*\[\\\x7f]")


class WorktreeError(Exception):
    """Raised when a git worktree operation fails.

    The message is user-facing and surfaced verbatim in the
    ``host.*_worktree_result`` frame's ``error`` field.

    :param message: Human-readable failure reason, e.g.
        ``"not a git repository: /tmp/x"``.
    """

    def __init__(self, message: str) -> None:
        """Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


def validate_branch_name(name: str) -> None:
    """Validate a git branch name against ``git check-ref-format`` rules.

    :param name: Proposed branch name, e.g. ``"feature/login"``.
    :raises WorktreeError: If the name is empty or violates any
        ref-format rule. The message names the specific violation.
    """
    if not name:
        raise WorktreeError("branch name must not be empty")
    if name.startswith("-"):
        raise WorktreeError(f"branch name must not start with '-': {name!r}")
    if name.startswith("/") or name.endswith("/"):
        raise WorktreeError(f"branch name must not start or end with '/': {name!r}")
    if name.endswith("."):
        raise WorktreeError(f"branch name must not end with '.': {name!r}")
    if any(part.endswith(".lock") for part in name.split("/")):
        raise WorktreeError(f"branch name path components must not end with '.lock': {name!r}")
    if ".." in name:
        raise WorktreeError(f"branch name must not contain '..': {name!r}")
    if "//" in name:
        raise WorktreeError(f"branch name must not contain '//': {name!r}")
    if "@{" in name:
        raise WorktreeError(f"branch name must not contain '@{{': {name!r}")
    if name == "@":
        raise WorktreeError("branch name must not be '@'")
    if _INVALID_BRANCH_CHARS.search(name):
        raise WorktreeError(
            f"branch name {name!r} contains an invalid character; spaces, "
            f"control characters, and any of ~ ^ : ? * [ \\ are not allowed"
        )
    # No path component may start with '.' (e.g. ".hidden" or "a/.b").
    if any(part.startswith(".") for part in name.split("/")):
        raise WorktreeError(f"branch name path components must not start with '.': {name!r}")


def _sanitize_dirname(branch_name: str) -> str:
    """Derive a single-segment directory name from a branch name.

    Slashes collapse to ``-`` so the worktree lives in one directory.

    :param branch_name: Validated branch name, e.g. ``"feature/login"``.
    :returns: Filesystem-safe single segment, e.g. ``"feature-login"``.
    """
    return branch_name.strip("/").replace("/", "-")


def _run_git(
    args: list[str],
    *,
    cwd: str,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, returning the completed process.

    :param args: Git argv *after* ``git``, e.g.
        ``["rev-parse", "--show-toplevel"]``. Passed as a list so no
        shell parsing occurs.
    :param cwd: Working directory to run git in, e.g.
        ``"/Users/alice/myrepo"``.
    :returns: The completed process with captured text stdout/stderr.
    :raises WorktreeError: If git is not installed, or the command
        exceeds :data:`_GIT_TIMEOUT_S`.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed on the host") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git command timed out after {_GIT_TIMEOUT_S:.0f}s") from exc


def _git_error(label: str, result: subprocess.CompletedProcess[str]) -> WorktreeError:
    """Build a WorktreeError from a failed git command.

    Includes the exit code (always present) and stderr when non-empty,
    so no invented "unknown error" fallback is needed.

    :param label: What failed, e.g. ``"git worktree add failed"``.
    :param result: The completed process with a non-zero return code.
    :returns: A :class:`WorktreeError` with code + stderr detail.
    """
    detail = result.stderr.strip()
    suffix = f": {detail}" if detail else ""
    return WorktreeError(f"{label} (exit {result.returncode}){suffix}")


def _main_work_tree(repo_path: str) -> str:
    """Resolve the MAIN work tree for any path inside a git repo.

    ``git worktree list --porcelain`` enumerates every work tree of the
    repository; its first entry is always the main one (the checkout all
    linked worktrees share). Run from ``repo_path``, this resolves the
    same main work tree whether the user picked the main checkout, a
    subdirectory, or a *linked worktree* — so a new worktree is always
    created as a sibling of the MAIN repo (e.g.
    ``…/myrepo-worktrees/<branch>``) rather than nested inside a worktree
    the session happened to start in (which ``rev-parse --show-toplevel``
    would produce: ``…/myrepo-worktrees/feature-worktrees/<branch>``).

    :param repo_path: Absolute path inside a git repository — the
        directory the user picked, e.g.
        ``"/Users/alice/myrepo-worktrees/feature"``.
    :returns: Absolute path of the main work tree, e.g.
        ``"/Users/alice/myrepo"``.
    :raises WorktreeError: If ``repo_path`` is not a directory or not
        inside a git work tree.
    """
    if not Path(repo_path).is_dir():
        raise WorktreeError(f"path is not a directory: {repo_path}")
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        raise WorktreeError(f"not a git repository: {repo_path}")
    for line in result.stdout.splitlines():
        # Porcelain format: the first record's ``worktree <path>`` line is
        # the main work tree; linked worktrees follow.
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip()
    raise WorktreeError(f"could not resolve main work tree for {repo_path}")


@dataclass
class WorktreeInfo:
    """One entry from ``git worktree list``.

    :param path: Absolute worktree directory, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Checked-out branch without the ``refs/heads/``
        prefix, e.g. ``"feature/login"``. ``None`` when the worktree
        is in detached-HEAD state.
    :param is_main: ``True`` for the repository's main work tree (the
        first ``git worktree list`` record), ``False`` for linked
        worktrees.
    :param detached: ``True`` when the worktree has a detached HEAD
        (no branch checked out).
    """

    path: str
    branch: str | None
    is_main: bool
    detached: bool


def list_worktrees(*, repo_path: str) -> list[WorktreeInfo]:
    """List the git worktrees of the repository containing ``repo_path``.

    Resolves the main work tree first (so a linked worktree resolves the
    same list as the main checkout), then parses
    ``git worktree list --porcelain``. The first record is always the
    main work tree; the rest are linked worktrees.

    :param repo_path: Absolute path inside a git repository — the
        directory the user picked, e.g. ``"/Users/alice/myrepo"``.
    :returns: One :class:`WorktreeInfo` per worktree, main first.
    :raises WorktreeError: If ``repo_path`` is not a directory or not
        inside a git work tree, or if ``git worktree list`` fails.
    """
    repo_root = _main_work_tree(repo_path)
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        raise _git_error("git worktree list failed", result)

    worktrees: list[WorktreeInfo] = []
    path: str | None = None
    branch: str | None = None
    detached = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            branch = None
            detached = False
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            detached = True
        elif line == "" and path is not None:
            # Blank line terminates a record.
            worktrees.append(
                WorktreeInfo(
                    path=path,
                    branch=branch,
                    is_main=not worktrees,
                    detached=detached,
                )
            )
            path = None
    # The porcelain output may omit a trailing blank line for the last record.
    if path is not None:
        worktrees.append(
            WorktreeInfo(path=path, branch=branch, is_main=not worktrees, detached=detached)
        )
    return worktrees


def _local_branch_exists(repo_root: str, branch_name: str) -> bool:
    """Return whether a local branch already exists in the repo.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Branch name to check, e.g. ``"feature/login"``.
    :returns: ``True`` if ``refs/heads/<branch_name>`` resolves.
    """
    return (
        _run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=repo_root,
        ).returncode
        == 0
    )


def _resolve_worktree_path(repo_root: str, branch_name: str) -> Path:
    """Compute a collision-free sibling worktree directory path.

    Places the worktree at
    ``<parent-of-repo-root>/<repo-name>-worktrees/<sanitized-branch>``,
    appending a numeric suffix if that path already exists on disk.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Validated branch name, e.g.
        ``"feature/login"``.
    :returns: A path that does not yet exist, e.g.
        ``Path("/Users/alice/myrepo-worktrees/feature-login")``.
    :raises WorktreeError: If no free path is found within
        :data:`_MAX_DIR_COLLISION_SUFFIX` attempts.
    """
    root = Path(repo_root)
    base_dir = root.parent / f"{root.name}-worktrees"
    dirname = _sanitize_dirname(branch_name)
    candidate = base_dir / dirname
    if not candidate.exists():
        return candidate
    for suffix in range(2, _MAX_DIR_COLLISION_SUFFIX + 1):
        candidate = base_dir / f"{dirname}-{suffix}"
        if not candidate.exists():
            return candidate
    raise WorktreeError(
        f"could not find a free worktree directory under {base_dir} "
        f"after {_MAX_DIR_COLLISION_SUFFIX} attempts"
    )


def _ensure_base_resolvable(repo_root: str, base_branch: str) -> None:
    """Make ``base_branch`` resolvable, fetching once if needed.

    If the base ref doesn't resolve locally (e.g. a remote-tracking
    branch not yet fetched), attempt a single ``git fetch`` and
    re-check. A fetch failure (offline) is not fatal on its own — the
    subsequent re-check produces the user-facing error.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param base_branch: Base ref the user requested, e.g. ``"main"``
        or ``"origin/main"``.
    :raises WorktreeError: If the base ref cannot be resolved even
        after a fetch attempt.
    """
    # --end-of-options forces git to treat the user-supplied base_branch as a
    # rev, never an option, so a value like "--exec-path" can't inject a git
    # flag (argv-only, no shell). Note: a bare "--" would not work here — git
    # rev-parse treats args after "--" as pathspecs, not revs.
    if (
        _run_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", base_branch], cwd=repo_root
        ).returncode
        == 0
    ):
        return
    # Best-effort fetch from the default remote, then re-verify.
    _run_git(["fetch"], cwd=repo_root)
    if (
        _run_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", base_branch], cwd=repo_root
        ).returncode
        != 0
    ):
        raise WorktreeError(f"base branch does not exist: {base_branch}")


@dataclass
class CreatedWorktree:
    """Result of a successful worktree creation.

    :param worktree_path: Absolute path of the created worktree
        directory, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: The branch checked out in the worktree, e.g.
        ``"feature/login"``.
    """

    worktree_path: str
    branch: str


def create_worktree(
    *,
    repo_path: str,
    branch_name: str,
    base_branch: str | None = None,
) -> CreatedWorktree:
    """Create a git worktree with a new branch checked out.

    Resolves the repo root, picks a collision-free sibling directory,
    and runs ``git worktree add -b`` (fetching once if ``base_branch``
    isn't locally resolvable).

    :param repo_path: Absolute path inside the source repo — the
        directory the user picked, e.g. ``"/Users/alice/myrepo"``.
    :param branch_name: New branch to create and check out, e.g.
        ``"feature/login"``.
    :param base_branch: Optional base ref, e.g. ``"main"``. ``None``
        branches from the repo's current ``HEAD``.
    :returns: The created worktree's path and branch.
    :raises WorktreeError: If the branch name is invalid, the path is
        not a git repo, the base ref can't be resolved, or
        ``git worktree add`` fails (e.g. the branch already exists).
    """
    validate_branch_name(branch_name)
    # Always create the worktree off the MAIN work tree, even when
    # ``repo_path`` is itself a linked worktree (e.g. the fork-resume
    # picker prefilled a worktree as the source). Otherwise the new
    # worktree would nest under the picked worktree
    # (``…/feature-worktrees/<branch>``); resolving to the main repo keeps
    # all worktrees as siblings (``…/myrepo-worktrees/<branch>``).
    repo_root = _main_work_tree(repo_path)
    # Friendly pre-check before git's raw "branch already exists" error.
    # We don't reuse the existing worktree: two sessions sharing one
    # working tree would clobber each other (designs/SESSION_GIT_WORKTREE.md).
    if _local_branch_exists(repo_root, branch_name):
        raise WorktreeError(
            f"a branch named {branch_name!r} already exists; choose a different branch name"
        )
    if base_branch is not None:
        _ensure_base_resolvable(repo_root, base_branch)
    worktree_path = _resolve_worktree_path(repo_root, branch_name)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    add_args = ["worktree", "add", "-b", branch_name, str(worktree_path)]
    if base_branch is not None:
        # --end-of-options: treat base_branch as a rev, never a git flag, so a
        # user-supplied value starting with '-' can't inject an option.
        add_args += ["--end-of-options", base_branch]
    result = _run_git(add_args, cwd=repo_root)
    if result.returncode != 0:
        raise _git_error("git worktree add failed", result)
    return CreatedWorktree(worktree_path=str(worktree_path), branch=branch_name)


def _main_repo_for_worktree(worktree_path: str) -> str:
    """Find the main repository work tree for a linked worktree.

    Uses ``git rev-parse --git-common-dir`` (which points at the
    shared ``.git`` of the main work tree) and returns that directory's
    parent. Run from inside the worktree so the relative result
    resolves correctly.

    :param worktree_path: Absolute path of a linked worktree, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :returns: Absolute path of the main repo work tree, e.g.
        ``"/Users/alice/myrepo"``.
    :raises WorktreeError: If ``worktree_path`` is missing or not part
        of a git repository.
    """
    if not Path(worktree_path).exists():
        raise WorktreeError(f"worktree path does not exist: {worktree_path}")
    result = _run_git(["rev-parse", "--git-common-dir"], cwd=worktree_path)
    if result.returncode != 0:
        raise WorktreeError(f"not a git worktree: {worktree_path}")
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (Path(worktree_path) / common_dir).resolve()
    return str(common_dir.parent)


def remove_worktree(
    *,
    worktree_path: str,
    branch: str | None = None,
    delete_branch: bool = False,
) -> None:
    """Remove a git worktree and optionally delete its branch.

    Removes the directory with ``--force``, then (if requested) deletes
    the branch — in that order, since git refuses to delete a branch
    still checked out in a linked worktree. ``git worktree remove``
    refuses to remove the main work tree.

    :param worktree_path: Absolute path of the worktree to remove,
        e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch to delete when ``delete_branch`` is
        ``True``, e.g. ``"feature/login"``. ``None`` skips branch
        deletion.
    :param delete_branch: When ``True``, run ``git branch -D`` on
        ``branch`` after removing the worktree directory.
    :raises WorktreeError: If the worktree path is missing/invalid, or
        a git command fails.
    """
    main_repo = _main_repo_for_worktree(worktree_path)
    remove_result = _run_git(
        ["worktree", "remove", "--force", worktree_path],
        cwd=main_repo,
    )
    if remove_result.returncode != 0:
        raise _git_error("git worktree remove failed", remove_result)
    if delete_branch and branch is not None:
        branch_result = _run_git(["branch", "-D", branch], cwd=main_repo)
        if branch_result.returncode != 0:
            raise _git_error("git branch -D failed", branch_result)
