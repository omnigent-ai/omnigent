"""Direct unit tests for the shared native-bridge security helpers.

``omnigent.native_bridge_security.ensure_secure_dir`` is the single
ancestor-walk that both the claude-native and cursor-native bridges call
through their thin ``_ensure_secure_dir`` wrappers. The bridge suites
exercise it indirectly via ``prepare_bridge_dir`` /
``build_cursor_native_spawn_env``; this module covers ``ensure_secure_dir``
itself so each rejection branch is locked in once, at the source, rather
than only through a bridge.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from omnigent.native_bridge_security import absolute_syntactic_path, ensure_secure_dir


def test_ensure_secure_dir_creates_owner_only_chain(tmp_path: Path) -> None:
    """
    A fresh chain below the trusted parent is created mode ``0o700``.

    Every intermediate ancestor the walk creates must be owner-only, not
    a default-umask ``0o755`` — ``/tmp`` is shared, and these dirs hold
    bearer tokens / tmux targets.
    """
    target = tmp_path / f"omnigent-{os.getuid()}" / "native" / "leaf"

    ensure_secure_dir(target, trusted_parent=tmp_path)

    assert target.is_dir()
    for ancestor in (target, target.parent, target.parent.parent):
        mode = stat.S_IMODE(ancestor.stat().st_mode)
        assert mode == 0o700, f"{ancestor} has mode {oct(mode)}, expected 0o700"


def test_ensure_secure_dir_rejects_not_a_directory_ancestor(tmp_path: Path) -> None:
    """
    A regular file pre-created at an ancestor path is refused.

    ``mkdir(parents=True, exist_ok=True)`` raises ``FileExistsError`` /
    ``NotADirectoryError`` here, but the secure walk must turn it into an
    explicit, attributable refusal rather than ever trusting a non-dir
    sitting on the bridge path. This not-a-directory branch is untested
    by either bridge suite, so it's covered directly here.
    """
    # Layout: tmp_path (trusted) -> ancestor (a regular FILE) -> ... -> leaf.
    ancestor_file = tmp_path / f"omnigent-{os.getuid()}"
    ancestor_file.write_text("not a dir", encoding="utf-8")
    target = ancestor_file / "native" / "leaf"

    with pytest.raises(RuntimeError, match="not a directory"):
        ensure_secure_dir(target, trusted_parent=tmp_path)


def test_ensure_secure_dir_rejects_not_a_directory_leaf(tmp_path: Path) -> None:
    """
    A regular file pre-created at the *leaf* target itself is refused.

    The walk includes ``target`` in its ancestor list, so a file sitting
    exactly where the bridge dir should be must also be rejected as
    "not a directory", not silently accepted.
    """
    target = tmp_path / "leaf-file"
    target.write_text("not a dir", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a directory"):
        ensure_secure_dir(target, trusted_parent=tmp_path)


def test_ensure_secure_dir_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    """
    A pre-created symlinked ancestor is refused, not followed.

    Direct analog of the bridge symlink tests, asserted at the shared
    helper so the rejection is pinned independent of any caller.
    """
    attacker_dir = tmp_path / "attacker-controlled"
    attacker_dir.mkdir(mode=0o700)
    symlink = tmp_path / f"omnigent-{os.getuid()}"
    symlink.symlink_to(attacker_dir, target_is_directory=True)
    target = symlink / "native" / "leaf"

    with pytest.raises(RuntimeError, match="symlink"):
        ensure_secure_dir(target, trusted_parent=tmp_path)

    # Refusal happens at the symlink, before anything is created through it.
    assert list(attacker_dir.iterdir()) == []


def test_ensure_secure_dir_rejects_target_outside_trusted_parent(tmp_path: Path) -> None:
    """
    A target that isn't below the trusted parent is refused.

    The walk stops when it reaches the filesystem root without ever
    hitting ``trusted_parent``; that must raise rather than fall through
    and start creating dirs at an arbitrary location.
    """
    trusted_parent = tmp_path / "trusted"
    trusted_parent.mkdir(mode=0o700)
    # Sibling of the trusted parent — not below it.
    target = tmp_path / "elsewhere" / "leaf"

    with pytest.raises(RuntimeError, match="not under trusted parent"):
        ensure_secure_dir(target, trusted_parent=trusted_parent)


def test_ensure_secure_dir_repairs_loose_permissions(tmp_path: Path) -> None:
    """
    An owned ancestor with group/other bits set is reset to ``0o700``.

    The walk repairs (rather than rejects) a directory it owns whose mode
    drifted to e.g. ``0o755`` — only foreign-owned / symlinked / non-dir
    ancestors are fatal.
    """
    loose = tmp_path / f"omnigent-{os.getuid()}"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    target = loose / "native" / "leaf"

    ensure_secure_dir(target, trusted_parent=tmp_path)

    assert stat.S_IMODE(loose.stat().st_mode) == 0o700
    assert target.is_dir()


def test_absolute_syntactic_path_does_not_follow_symlinks(tmp_path: Path) -> None:
    """
    ``absolute_syntactic_path`` normalizes without resolving symlinks.

    Security validation must ``lstat`` symlinked ancestors, so this
    helper deliberately avoids ``Path.resolve`` — a symlink in the path
    must survive normalization rather than be collapsed to its target.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    result = absolute_syntactic_path(link / ".." / "link")

    # ".." is collapsed syntactically but the symlink is NOT resolved to
    # ``real`` — the returned path still names ``link``.
    assert result == link
    assert result != real
