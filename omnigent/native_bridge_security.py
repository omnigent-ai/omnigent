"""Shared filesystem-security helpers for native-bridge directories.

Native bridges (claude-native, cursor-native, …) rendezvous through a
per-uid directory tree under a shared *trusted parent* (e.g. ``/tmp``).
Those trees store bearer tokens and tmux targets, so the whole ancestor
chain below the trusted parent must be owner-only and free of symlinks
or foreign-owned directories.

``Path.mkdir(mode=0o700, parents=True, exist_ok=True)`` only applies the
mode to the leaf and silently trusts any pre-existing ancestor — on a
shared host an attacker could pre-create an intermediate ancestor as a
symlink (or a world-writable directory) and redirect the bridge tree.
This module walks each ancestor from the trusted parent down to the
target, creating new ones with mode ``0o700`` and rejecting any existing
ancestor that is a symlink, not a directory, owned by a different uid,
or that has group/other permission bits set.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def absolute_syntactic_path(path: Path) -> Path:
    """
    Return an absolute path without following symlinks.

    Security validation needs to inspect symlinked ancestors with
    ``lstat``. ``Path.resolve`` would follow an existing symlink before
    that inspection, so this helper only expands ``~`` and normalizes
    ``.`` / ``..`` components.

    :param path: Path to normalize, e.g. ``Path("~/.omnigent/x")``.
    :returns: Absolute path with syntactic normalization applied.
    """
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def ensure_secure_dir(target: Path, *, trusted_parent: Path) -> None:
    """
    Create or validate ``target`` as an owner-only directory chain.

    Walks each ancestor from ``trusted_parent`` (exclusive) down to
    ``target`` (inclusive), creating new ones with mode ``0o700`` and
    rejecting any existing ancestor that is a symlink, not a directory,
    owned by a different uid, or has group/other permission bits set.
    Wrong-but-repairable modes on directories we own are reset to
    ``0o700``.

    :param target: Final bridge directory path to ensure, e.g.
        ``Path("/tmp/omnigent-501/cursor-native/abc")``.
    :param trusted_parent: Pre-existing, trusted anchor at which ancestor
        validation stops, e.g. ``Path("/tmp")``.
    :raises RuntimeError: If ``target`` is not below ``trusted_parent`` or
        any ancestor fails validation.
    """
    target = absolute_syntactic_path(target)
    trusted_parent = absolute_syntactic_path(trusted_parent)
    ancestors: list[Path] = []
    cur = target
    while cur != trusted_parent and cur != cur.parent:
        ancestors.append(cur)
        cur = cur.parent
    if cur != trusted_parent:
        raise RuntimeError(f"bridge dir {target!s} is not under trusted parent {trusted_parent!s}")
    ancestors.reverse()
    my_uid = os.getuid()
    for ancestor in ancestors:
        try:
            os.mkdir(ancestor, mode=0o700)
            continue
        except FileExistsError:
            pass
        st = os.lstat(ancestor)
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing to use bridge ancestor {ancestor!s}: is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"refusing to use bridge ancestor {ancestor!s}: not a directory")
        if st.st_uid != my_uid:
            raise RuntimeError(
                f"refusing to use bridge ancestor {ancestor!s}: owned by uid "
                f"{st.st_uid}, not current user ({my_uid})"
            )
        if (st.st_mode & 0o077) != 0:
            os.chmod(ancestor, 0o700)
