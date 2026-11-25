"""The REST file search's budgeted directory walk, shared with the workspace API.

The walk never follows symlinks below its root, skips ``.git``, and visits
dependency directories such as ``node_modules`` last, so they can't use up
the scan budget before the real tree is covered.
"""

from __future__ import annotations

import os
import re
import stat
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

from omnigent.entities.environment_filesystem import FilesystemEntry
from omnigent.inner._cwd_scan import _DEFAULT_DEPRIORITIZED_DIRS


def walk_search(
    root: Path,
    query: str,
    include: Sequence[re.Pattern[str]],
    exclude: Sequence[re.Pattern[str]],
    limit: int,
    *,
    budget: int,
    descend: Callable[[str], bool] | None = None,
    emit: Callable[[str], bool] | None = None,
    describe_link: Callable[[str, os.stat_result], FilesystemEntry] | None = None,
) -> tuple[list[FilesystemEntry], bool]:
    """Walk *root* for entries whose name or relative path contains *query*.

    The REST file search's walk: budgeted, never following symlinks below
    *root*, skipping ``.git`` and visiting dependency directories last.

    :param root: Directory the walk starts at; result paths are relative to it.
    :param query: Lowercased substring.
    :param include: Compiled include globs; when any, a path must match one.
    :param exclude: Compiled exclude globs, which prune directories too.
    :param limit: Maximum entries to collect.
    :param budget: Entries the walk may visit before it stops, truncated.
    :param descend: Returns False for a directory the walk must neither
        return nor enter, given its path relative to *root*.
    :param emit: Returns False for a path the walk must not return.
    :param describe_link: Describes a symlink the walk returns, given its path and
        own ``lstat``; without it, a link is described by what it points to.
    :returns: Entries sorted by path, and whether the scan budget ran out.
    """
    results: list[FilesystemEntry] = []
    # State the two-pass walk mutates via closures (rebound through the
    # `nonlocal` in scan()). deferred is a FIFO of noise-dir roots to drain
    # in pass 2.
    deferred: deque[str] = deque()
    scanned = 0
    truncated = False
    stop = False

    # os.walk is handed the absolute root, so an entry's path relative to
    # it is a slice of dirpath -- no per-entry relpath(), whose abspath()
    # work used to dominate the walk's runtime.
    # rstrip: a bare root ("/", "C:\\") already ends in the separator the
    # slice skips.
    cut = len(str(root).rstrip(os.sep)) + 1

    def rel(dirpath: str, name: str) -> str:
        # Entries always use '/', like the git-index paths they merge with.
        rel_dir = dirpath[cut:]
        if os.sep != "/":
            rel_dir = rel_dir.replace(os.sep, "/")
        return f"{rel_dir}/{name}" if rel_dir else name

    def match(dirpath: str, name: str, *, is_dir: bool) -> None:
        # A directory carries no byte size; a file stats for size + mtime.
        p = rel(dirpath, name)
        if exclude and any(r.match(p) for r in exclude):
            return
        if include and not any(r.match(p) for r in include):
            return
        if query not in name.lower() and query not in p.lower():
            return
        if emit is not None and not emit(p):
            return
        if describe_link is not None:
            try:
                own = os.lstat(os.path.join(dirpath, name))
            except OSError:
                own = None
            if own is not None and stat.S_ISLNK(own.st_mode):
                results.append(describe_link(p, own))
                return
        try:
            st = (Path(dirpath) / name).stat()
            size: int | None = None if is_dir else st.st_size
            mtime: int | None = int(st.st_mtime)
        except OSError:
            size = None
            mtime = None
        results.append(
            FilesystemEntry(
                id=p,
                name=name,
                path=p,
                type="directory" if is_dir else "file",
                bytes=size,
                modified_at=mtime,
            )
        )

    def scan(start: str, defer: bool) -> None:
        # A query matching little or nothing never fills the result cap, so
        # the walk needs its own bound. Counted per entry: a per-directory
        # check lets one huge directory overshoot before `truncated` trips.
        nonlocal scanned, truncated, stop
        for dirpath, dirnames, filenames in os.walk(start):
            kept = []
            for d in sorted(dirnames):
                if d == ".git":
                    # Git's own store is never a search target, and in a
                    # plain clone its reflogs alone can outnumber the tree.
                    continue
                full = os.path.join(dirpath, d)
                dp = rel(dirpath, d)
                if any(r.match(dp) for r in exclude):
                    continue
                if descend is not None and not descend(dp):
                    continue
                if defer and d in _DEFAULT_DEPRIORITIZED_DIRS:
                    # Match the dir now, but walk its subtree later (pass 2)
                    # so it can't starve the real tree of scan budget. Never
                    # defer a symlinked dir: os.walk(root) follows a top-level
                    # symlink, so a committed 'node_modules -> ..' would let
                    # pass 2 escape the workspace. os.walk(followlinks=False)
                    # never crosses symlinks mid-tree; deferring only real
                    # dirs keeps that boundary intact.
                    if not os.path.islink(full):
                        deferred.append(full)
                kept.append(d)
            dirnames[:] = [d for d in kept if not (defer and d in _DEFAULT_DEPRIORITIZED_DIRS)]
            for dname in kept:
                # Count then check `> budget`, not `>= budget`: a tree of
                # exactly `budget` entries is fully enumerable and must not
                # report truncated.
                scanned += 1
                if scanned > budget:
                    truncated = True
                    stop = True
                    return
                match(dirpath, dname, is_dir=True)
                if len(results) >= limit:
                    stop = True
                    return
            for fname in sorted(filenames):
                scanned += 1
                if scanned > budget:
                    truncated = True
                    stop = True
                    return
                match(dirpath, fname, is_dir=False)
                if len(results) >= limit:
                    stop = True
                    return

    # Pass 1 walks the real tree and defers dependency/cache subtrees;
    # reordering siblings isn't enough, because a deep node_modules nested
    # under an earlier-sorted real dir would still swallow the whole budget
    # before the walk reached a later top-level dir. Pass 2 drains the
    # deferred roots only if budget remains. Mirrors the runner's walk.
    scan(str(root), True)
    while deferred and not stop:
        scan(deferred.popleft(), False)
    # When the walk stops early it is always because scan() tripped the
    # budget (which sets truncated) or the result limit (signaled by
    # has_more); the loop exits only once deferred is drained or stop is
    # set, so no extra truncation flag is needed here.

    results.sort(key=lambda entry: entry.path)
    return results, truncated
