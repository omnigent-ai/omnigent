"""Which entries of a directory listing Git ignores, through the hardened runner.

ListDirectory marks them with ``Entry.ignored``, or leaves them out when asked to.
Git decides with its own rules: ``.gitignore`` files at every level,
``.git/info/exclude`` and the global excludes file. Tracked files are never ignored.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from omnigent.host.workspace_git.runner import (
    LISTING_OUTPUT_BYTES,
    Deadline,
    GitRepository,
    failure,
)

# check-ignore matches its paths as pathspecs against the index, so a name like "*.log"
# would match a tracked file. "./" keeps a leading ":" from reading as magic, and a
# backslash before each glob character makes Git read the name literally.
_PREFIX = "./"
_GLOB_CHARACTERS = re.compile(r"([*?\[\\])")


def _literal(path: str) -> str:
    """``path`` as a pathspec that matches only itself."""
    return _PREFIX + _GLOB_CHARACTERS.sub(r"\\\1", path)


async def ignored_paths(root: Path, paths: Sequence[str], deadline: Deadline) -> set[str]:
    """The ``paths`` Git ignores, relative to the workspace ``root``.

    Outside a Git repository nothing is ignored.

    :param root: The workspace directory, resolved.
    :param paths: Workspace-relative paths, e.g. ``["build", "src/main.py"]``.
    :param deadline: The RPC's deadline, shared by every command.
    :raises GitError: When Git can't answer.
    """
    if not paths:
        return set()
    repository = await GitRepository.discover(root, deadline)
    if repository is None:
        return set()
    # Git echoes each path as it was given, so answers map back through the escaped form.
    asked = {_literal(path): path for path in paths}
    stdin = b"".join(os.fsencode(query) + b"\0" for query in asked)
    output = await repository.run_unchecked(
        ["check-ignore", "-z", "--stdin"],
        deadline=deadline,
        stdin=stdin,
        stdout_bytes=LISTING_OUTPUT_BYTES,
    )
    # Exit 1 means none of the paths is ignored.
    if output.returncode == 1:
        return set()
    if not output.success:
        raise failure(output, "check-ignore")
    records = (os.fsdecode(record) for record in output.stdout.split(b"\0") if record)
    return {asked[record] for record in records if record in asked}
