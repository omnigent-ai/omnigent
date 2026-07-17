"""
Safe, read-only browsing of a resolved skill's on-disk resource tree.

The Skills page lets a user browse a skill's full directory (its ``SKILL.md``
plus the conventional ``references/``, ``scripts/``, ``assets/`` subdirs). These
helpers back that feature. They are deliberately *pure* (a directory in, plain
data out) so the containment rules can be unit-tested without a server:

- Everything is resolved **against the winner's ``skill_dir``**. Absolute paths,
  ``..`` traversal, and symlinks that escape the skill directory are rejected —
  a skill must never become a window onto the wider workspace filesystem.
- The tree walk skips symlinks entirely (a symlinked file/dir inside the skill
  is not followed), so a crafted skill can't leak host files by linking to them.
- Reads are size-capped and binary files are reported as metadata only; the
  caller decides how to present a non-previewable resource.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# A skill's whole point is small instruction/reference text. Cap a single file
# read well below anything that would bloat a JSON response; larger files are
# reported as non-previewable (download-only) metadata.
MAX_SKILL_FILE_BYTES = 512 * 1024  # 512 KiB

# Hard cap on the number of tree entries returned, so a pathological skill dir
# can't produce an unbounded catalog response.
MAX_SKILL_TREE_ENTRIES = 2000


@dataclass(frozen=True)
class SkillFileNode:
    """One entry in a skill's resource tree."""

    #: POSIX-style path relative to the skill root (e.g. ``references/x.md``).
    path: str
    #: ``"file"`` or ``"dir"``.
    kind: str
    #: File size in bytes; ``None`` for directories.
    size: int | None


class SkillFileError(Exception):
    """A skill-file request was invalid or refused (traversal, missing, etc.)."""

    def __init__(self, message: str, *, status: int) -> None:
        """
        :param message: Human-readable reason.
        :param status: The HTTP status the route should map this to
            (400 bad request, 404 not found, 413 too large).
        """
        super().__init__(message)
        self.message = message
        self.status = status


def list_skill_tree(skill_dir: Path) -> list[SkillFileNode]:
    """
    Walk ``skill_dir`` and return its files + directories, sorted for display.

    Ordering matches a familiar file browser: within each directory, sub-dirs
    first (then files), each alphabetical, case-insensitive. Symlinks are
    skipped so the tree can never point outside the skill. Returns an empty list
    for a missing or empty directory (an empty tree is not an error).

    :param skill_dir: Absolute path to the resolved winner's skill directory.
    :returns: Up to :data:`MAX_SKILL_TREE_ENTRIES` nodes, deterministically
        ordered (parents before children).
    """
    root = skill_dir.resolve()
    if not root.is_dir():
        return []
    nodes: list[SkillFileNode] = []

    def _walk(directory: Path) -> None:
        if len(nodes) >= MAX_SKILL_TREE_ENTRIES:
            return
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda p: (not _is_real_dir(p), p.name.lower()),
            )
        except OSError:
            return
        for child in children:
            if len(nodes) >= MAX_SKILL_TREE_ENTRIES:
                return
            # Skip symlinks outright — following one could leave the skill dir.
            if child.is_symlink():
                continue
            rel = child.relative_to(root).as_posix()
            if child.is_dir():
                nodes.append(SkillFileNode(path=rel, kind="dir", size=None))
                _walk(child)
            elif child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    continue
                nodes.append(SkillFileNode(path=rel, kind="file", size=size))

    _walk(root)
    return nodes


def _is_real_dir(path: Path) -> bool:
    """Return True only for a non-symlink directory (symlinks sort as files)."""
    return path.is_dir() and not path.is_symlink()


def resolve_skill_file(skill_dir: Path, rel_path: str) -> Path:
    """
    Resolve ``rel_path`` inside ``skill_dir``, refusing anything unsafe.

    Guards, in order: the path must be non-empty and relative (no leading ``/``);
    after resolution it must stay inside the skill root (blocks ``..`` and
    symlink escapes); no path component may be a symlink; and the target must be
    an existing regular file (a directory is not readable content).

    :param skill_dir: Absolute path to the resolved winner's skill directory.
    :param rel_path: Caller-supplied relative path, e.g. ``references/x.md``.
    :returns: The safe absolute path to read.
    :raises SkillFileError: If the path is absolute/empty (400), escapes the
        skill dir or traverses a symlink (400), or does not resolve to an
        existing file (404).
    """
    if not rel_path:
        raise SkillFileError("path must not be empty", status=400)
    parsed = PurePosixPath(rel_path)
    if parsed.is_absolute():
        raise SkillFileError("path must be relative", status=400)
    if any(part == ".." for part in parsed.parts):
        raise SkillFileError("path traversal not allowed", status=400)

    root = skill_dir.resolve()
    # Reject a symlink at any component *before* resolving, so a symlinked dir
    # inside the skill can't be used as a stepping stone out.
    probe = root
    for part in parsed.parts:
        probe = probe / part
        if probe.is_symlink():
            raise SkillFileError("path traversal not allowed", status=400)

    resolved = (root / parsed).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise SkillFileError("path traversal not allowed", status=400)
    if not resolved.is_file():
        raise SkillFileError(f"file not found: {rel_path}", status=404)
    return resolved


@dataclass(frozen=True)
class SkillFileContent:
    """The result of a safe skill-file read."""

    path: str
    size: int
    #: True when the bytes decode as UTF-8 text (previewable); else binary.
    is_text: bool
    #: True when the file exceeds the preview size cap (not read into ``text``).
    too_large: bool
    #: UTF-8 text, present only when ``is_text and not too_large``.
    text: str | None


def read_skill_file(skill_dir: Path, rel_path: str) -> SkillFileContent:
    """
    Read a resolved skill file for preview, capped and text/binary-aware.

    Files over :data:`MAX_SKILL_FILE_BYTES` return metadata with
    ``too_large=True`` and no ``text`` (the UI shows a download-only state).
    Otherwise the bytes are decoded as UTF-8; if that fails the file is binary
    and ``text`` stays ``None`` (``is_text=False``).

    :param skill_dir: Absolute path to the resolved winner's skill directory.
    :param rel_path: Caller-supplied relative path.
    :returns: The file's content + metadata.
    :raises SkillFileError: Propagated from :func:`resolve_skill_file`.
    """
    resolved = resolve_skill_file(skill_dir, rel_path)
    size = resolved.stat().st_size
    rel = os.path.relpath(resolved, skill_dir.resolve())
    rel_posix = PurePosixPath(rel).as_posix()
    if size > MAX_SKILL_FILE_BYTES:
        return SkillFileContent(
            path=rel_posix, size=size, is_text=False, too_large=True, text=None
        )
    data = resolved.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return SkillFileContent(
            path=rel_posix, size=size, is_text=False, too_large=False, text=None
        )
    return SkillFileContent(path=rel_posix, size=size, is_text=True, too_large=False, text=text)
