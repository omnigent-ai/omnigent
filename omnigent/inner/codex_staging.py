"""Staging-root helpers for the wrapped codex executor's private CODEX_HOMEs.

The wrapped codex executor gives every conversation a private ``CODEX_HOME``
(so the user's real ``~/.codex/`` is never touched), and codex publishes each
skill's ``SKILL.md`` path — a path inside that home — in the model's skill
manifest. Two constraints follow:

- the staged home must live **outside the session workspace**, so it never
  shows up as an untracked directory in the user's checkout; and
- the published ``skills/`` subtree must stay **readable from the session's
  sandboxed tools**, while the rest of the home (``auth.json``,
  ``config.toml``, session logs) stays hidden.

Both hang off one well-known location: homes are staged under
:func:`codex_home_staging_root` in the system temp dir, and sandbox backends
re-expose exactly the ``skills/`` subtree of each staged home via
:func:`staged_codex_skill_dirs`.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

# Prefix of each per-conversation home created under the staging root. Also
# what identifies an Omnigent-private codex home to nested launches (see
# ``_is_omnigent_private_codex_home`` in ``codex_executor``).
CODEX_HOME_PREFIX = "omnigent-codex-home-"


def _staging_root_path() -> Path:
    # Per-uid name: the system temp dir is shared, so a fixed name could be
    # squatted by another user. Together with the ownership check in
    # :func:`staged_codex_skill_dirs` this keeps other users' trees out of
    # the sandbox mount set. Windows temp dirs are already per-user.
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()) / f"omnigent-codex-homes{suffix}"


def codex_home_staging_root() -> Path:
    """Create-and-return the root that per-conversation CODEX_HOMEs live under.

    :returns: The per-user staging root, private (``0o700``) to the current
        user.
    :raises OSError: When the root cannot be created (e.g. an unwritable
        system temp dir); callers fall back to a plain temp-dir home, which
        keeps the session bootable at the cost of sandbox skill exposure.
    """
    root = _staging_root_path()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A pre-existing root may carry a permissive umask-derived mode; tighten
    # it so the globber's safety check keeps accepting the root.
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    return root


def staged_codex_skill_dirs() -> list[Path]:
    """The ``skills/`` dirs of currently-staged codex homes, for sandbox exposure.

    Sandbox backends bind these read-only so the ``$CODEX_HOME/skills/...``
    paths codex publishes in its skill manifest resolve inside the tool
    namespace. Only the ``skills/`` subtree is ever returned — siblings such
    as ``auth.json`` and ``config.toml`` stay unmounted. The root is ignored
    entirely unless it is a directory owned by the current user without
    group/other write access, so a squatted or loosened root cannot inject
    content into sandboxes (fail closed).

    :returns: Sorted list of existing ``<staging-root>/<home>/skills`` dirs;
        empty when the root is absent or fails the safety check.
    """
    root = _staging_root_path()
    try:
        root_stat = root.stat()
    except OSError:
        return []
    if not stat.S_ISDIR(root_stat.st_mode):
        return []
    if hasattr(os, "getuid") and root_stat.st_uid != os.getuid():
        return []
    if root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return []
    skill_dirs: list[Path] = []
    try:
        homes = sorted(root.iterdir())
    except OSError:
        return []
    for home in homes:
        if not home.name.startswith(CODEX_HOME_PREFIX):
            continue
        skills = home / "skills"
        with contextlib.suppress(OSError):
            if skills.is_dir():
                skill_dirs.append(skills)
    return skill_dirs
