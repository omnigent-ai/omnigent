"""Private Codex homes and explicitly granted, session-owned skill directories.

Homes stay outside the workspace. Skills live in a separate directory granted
only to their owning session; no sandbox discovers grants by scanning temp homes.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# Prefix of each per-conversation home created under the staging root. Also
# what identifies an Omnigent-private codex home to nested launches (see
# ``_is_omnigent_private_codex_home`` in ``codex_executor``).
CODEX_HOME_PREFIX = "omnigent-codex-home-"
CODEX_SKILLS_PREFIX = "omnigent-codex-skills-"


def _staging_root_path() -> Path:
    # The shared temp root must not route private homes through another user.
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()).resolve() / f"omnigent-codex-homes{suffix}"


def codex_home_staging_root() -> Path:
    """Create-and-return the root that per-conversation CODEX_HOMEs live under.

    :returns: The per-user staging root, verified private (``0o700``) to the
        current user.
    :raises OSError: When the root cannot be created (e.g. an unwritable
        system temp dir), or exists but is not a real directory owned by the
        current user without group/other write access. Callers fall back to a
        plain unpredictable temp-dir home. Skills use a separate directory,
        so this fallback does not change their sandbox visibility.
    """
    root = _staging_root_path()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not hasattr(os, "getuid"):
        # Windows temp dirs are already per-user; POSIX ownership semantics
        # don't apply.
        return root
    # ``mkdir(exist_ok=True)`` silently accepts a pre-existing path — even a
    # symlink to a directory — and the name is predictable in a shared temp
    # dir, so another principal could have planted it first. Codex later
    # reads ``config.toml``/``auth.json`` from homes under this root by
    # pathname, so refuse anything that is not a real directory we own.
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise OSError(
            f"codex home staging root {root} is not a directory owned by the current user"
        )
    if root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        # A pre-existing root may carry a permissive umask-derived mode;
        # tighten it — failing loud when we cannot — so no other principal
        # can rename homes out from under live sessions.
        root.chmod(0o700)
    return root


def prepare_codex_skills_dir(path: Path) -> Path:
    """Validate and empty an owned skill directory without replacing its inode.

    Existing sandbox mounts keep this directory across harness restarts. Clear
    old contents so a narrower skill filter cannot retain previously loaded skills.
    """
    root = path.parent.resolve() / path.name
    root_stat = root.lstat()
    if (
        not root.name.startswith(CODEX_SKILLS_PREFIX)
        or not stat.S_ISDIR(root_stat.st_mode)
        or root.is_junction()
    ):
        raise OSError("Codex skills must use a dedicated session staging directory")
    if hasattr(os, "getuid") and (
        root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise OSError("Codex skills staging directory must be private to the current user")
    for child in root.iterdir():
        if child.is_junction():
            child.rmdir()
        elif child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)
    return root


def link_codex_skills_dir(link_path: Path, skills_dir: Path) -> None:
    """Link a skill-discovery entry to a directory without copying its target.

    Codex publishes resolved skill paths, keeping read grants tied to the target.

    :param link_path: Home-level ``skills`` entry or individual skill; must not exist yet.
    :param skills_dir: Session-owned skills directory or selected source directory.
    :raises OSError: When the platform can create neither a symlink nor, on
        Windows, a directory junction.
    """
    try:
        link_path.symlink_to(skills_dir, target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            # Windows refuses symlinks without Developer Mode or the symlink
            # privilege; a junction needs neither and resolves the same way.
            import _winapi

            _winapi.CreateJunction(str(skills_dir), str(link_path))
        else:
            raise
