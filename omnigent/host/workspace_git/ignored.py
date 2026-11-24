"""Which entries of a directory listing Git ignores, through the hardened runner.

ListDirectory marks them with ``Entry.ignored``, or leaves them out when asked to.
Git decides with its own rules: ``.gitignore`` files at every level,
``.git/info/exclude`` and the global excludes file. Tracked files are never ignored,
and neither is a folder that holds one.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from omnigent.host.workspace_git.runner import (
    LISTING_OUTPUT_BYTES,
    Deadline,
    GitRepository,
    failure,
)

# "./" keeps a leading ":" from reading as pathspec magic; names are otherwise raw, so
# ignore rules see them as they are. ls-files, with literal pathspecs, finds tracked ones.
_PREFIX = "./"


@dataclass(frozen=True)
class DirectoryIgnores:
    """Asks Git which entries of one listed directory it ignores.

    :param repository: The repository holding the directory, found from the directory
        itself, so a submodule or a folder behind a symlink answers for its own files.
    """

    repository: GitRepository

    @classmethod
    async def for_directory(cls, directory: Path, deadline: Deadline) -> DirectoryIgnores | None:
        """The directory's repository, or ``None`` outside one, where nothing is ignored."""
        repository = await GitRepository.discover(directory, deadline)
        return None if repository is None else cls(repository)

    async def ignored(self, names: Sequence[str], deadline: Deadline) -> set[str]:
        """The ``names`` Git ignores: entries of the directory, or paths below it.

        :raises GitError: When Git can't answer.
        """
        if not names:
            return set()
        matched = await self._matching_ignore_rules(names, deadline)
        if not matched:
            return set()
        return matched - await self._holding_tracked_files(matched, deadline)

    async def _matching_ignore_rules(self, names: Sequence[str], deadline: Deadline) -> set[str]:
        stdin = b"".join(os.fsencode(_PREFIX + name) + b"\0" for name in names)
        output = await self.repository.run_unchecked(
            ["check-ignore", "--no-index", "-z", "--stdin"],
            deadline=deadline,
            stdin=stdin,
            stdout_bytes=LISTING_OUTPUT_BYTES,
        )
        # Exit 1 means none of the names is ignored.
        if output.returncode == 1:
            return set()
        if not output.success:
            raise failure(output, "check-ignore")
        asked = set(names)
        records = (os.fsdecode(record) for record in output.stdout.split(b"\0") if record)
        return {name for record in records if (name := record.removeprefix(_PREFIX)) in asked}

    async def _holding_tracked_files(self, names: set[str], deadline: Deadline) -> set[str]:
        """The ``names`` that are tracked files, or folders holding a tracked file.

        Only tracked files the ignore rules cover can sit in an ignored name, so listing
        those keeps the answer as small as the overlap.
        """
        output = await self.repository.run(
            ["ls-files", "-z", "--cached", "--ignored", "--exclude-standard", "--", *names],
            deadline=deadline,
            stdout_bytes=LISTING_OUTPUT_BYTES,
        )
        tracked = (os.fsdecode(record) for record in output.split(b"\0") if record)
        return {folder for path in tracked for folder in _path_and_folders(path)} & names


def _path_and_folders(path: str) -> list[str]:
    """``"a/b/c"`` and the folders above it: ``["a", "a/b", "a/b/c"]``."""
    parts = path.split("/")
    return ["/".join(parts[: depth + 1]) for depth in range(len(parts))]
