"""Filesystem-security tests for the cursor-native bridge directory.

Mirrors ``tests/test_claude_native_bridge.py`` symlink-rejection +
owner-only coverage: cursor's bridge tree records the tmux socket/target
under a shared ``$TMPDIR``/``/tmp`` root, so its ancestor chain must be
validated (no symlinks, no foreign owners, ``0o700``) the same way
claude-native validates its bearer-token tree.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from omnigent.cursor_native_bridge import build_cursor_native_spawn_env


def test_spawn_env_refuses_symlinked_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pre-created symlinked bridge ancestor is refused, not silently followed.

    Without the shared ``_ensure_secure_dir`` walk, a plain
    ``mkdir(parents=True, exist_ok=True)`` happily traverses a symlinked
    intermediate dir, redirecting the bridge tree (including the tmux
    target it records) to a path an attacker controls. A regression that
    swaps the secure walk back to a plain mkdir would let this succeed.
    """
    # Layout: tmp_path is the trusted parent. Place a "cursor-native"
    # symlink that points at a separate attacker-controlled directory
    # before any spawn-env call runs.
    attacker_dir = tmp_path / "attacker-controlled"
    attacker_dir.mkdir()
    symlink = tmp_path / "cursor-native"
    symlink.symlink_to(attacker_dir, target_is_directory=True)

    monkeypatch.setattr("omnigent.cursor_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.cursor_native_bridge._BRIDGE_ROOT", symlink)

    with pytest.raises(RuntimeError, match="symlink"):
        build_cursor_native_spawn_env("conv_abc")

    # Confirm nothing was created inside the attacker-controlled directory
    # — the refusal happened before any per-session dir was made.
    assert list(attacker_dir.iterdir()) == []


def test_spawn_env_refuses_symlinked_intermediate_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A symlink at the *production* ``omnigent-<uid>`` ancestor is refused.

    The real bridge tree is ``$TMPDIR/omnigent-<uid>/cursor-native/<hash>``,
    so the trusted parent and the leaf's immediate parent are *not* the same
    directory — there's an intermediate ``omnigent-<uid>`` level in between.
    The other symlink test collapses that chain (its ``_BRIDGE_ROOT`` is a
    direct child of ``_TRUSTED_PARENT``), so a regression that only stat'd
    the leaf's immediate parent would still pass it. This test reproduces
    the multi-level layout and poisons the *intermediate* ancestor, which
    only a true ancestor walk from the trusted parent down can catch.

    A regression that checked just the leaf's immediate parent
    (``cursor-native``, here a real, locked-down dir) would silently follow
    the symlink and redirect the bridge tree (including the tmux target it
    records) into the attacker-controlled directory.
    """
    # Reproduce the real layout: tmp_path (trusted) -> omnigent-<uid>
    # (intermediate) -> cursor-native (root) -> <hash> (leaf). Only the
    # intermediate level is replaced with an attacker symlink; everything
    # below it would be perfectly fine if the walk stopped at the leaf's
    # immediate parent.
    intermediate_name = f"omnigent-{os.getuid()}"
    attacker_dir = tmp_path / "attacker-controlled"
    attacker_dir.mkdir(mode=0o700)
    intermediate = tmp_path / intermediate_name
    intermediate.symlink_to(attacker_dir, target_is_directory=True)

    monkeypatch.setattr("omnigent.cursor_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(
        "omnigent.cursor_native_bridge._BRIDGE_ROOT",
        intermediate / "cursor-native",
    )

    with pytest.raises(RuntimeError, match="symlink"):
        build_cursor_native_spawn_env("conv_abc")

    # The refusal must happen at the intermediate symlink, before anything
    # is created through it inside the attacker-controlled directory.
    assert list(attacker_dir.iterdir()) == []


def test_spawn_env_restricts_filesystem_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge directory chain is owner-only (``0o700``), not world-readable.

    ``$TMPDIR``/``/tmp`` is shared with other Unix users; the per-session
    bridge dir records the tmux socket/target. If its perms drift to a
    default ``0o755`` other users on the box can enter it. A regression
    here would be invisible without an explicit stat assertion.
    """
    monkeypatch.setattr("omnigent.cursor_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(
        "omnigent.cursor_native_bridge._BRIDGE_ROOT", tmp_path / "cursor-native"
    )

    env = build_cursor_native_spawn_env("conv_abc")
    bridge_dir = Path(env["HARNESS_CURSOR_NATIVE_BRIDGE_DIR"])

    dir_mode = stat.S_IMODE(bridge_dir.stat().st_mode)
    assert dir_mode == 0o700, (
        f"bridge dir at {bridge_dir} has mode {oct(dir_mode)}; "
        "expected 0o700 so other host users cannot enter it"
    )
    # The intermediate cursor-native root must be locked down too.
    root_mode = stat.S_IMODE((tmp_path / "cursor-native").stat().st_mode)
    assert root_mode == 0o700, (
        f"bridge root has mode {oct(root_mode)}; expected 0o700"
    )
