"""Which entries of a directory listing Git ignores, through the hardened runner.

ListDirectory marks them with ``Entry.ignored``, or leaves them out when asked to.
Git decides with its own rules: ``.gitignore`` files at every level,
``.git/info/exclude`` and the global excludes file. Tracked files are never ignored.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from omnigent.host.workspace_git.runner import (
    LISTING_OUTPUT_BYTES,
    Deadline,
    GitRepository,
    failure,
)

# The "./" keeps a name that starts with ":" from reading as pathspec magic.
_PREFIX = "./"


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
    stdin = b"".join(os.fsencode(_PREFIX + path) + b"\0" for path in paths)
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
    asked = set(paths)
    records = (os.fsdecode(record) for record in output.stdout.split(b"\0") if record)
    return {
        record.removeprefix(_PREFIX) for record in records if record.removeprefix(_PREFIX) in asked
    }
