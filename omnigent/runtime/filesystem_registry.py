"""Per-conversation filesystem-change registry.

Two concrete implementations are provided:

- :class:`GitFilesystemRegistry` — used when the workspace lives inside a git
  repository.  Baseline content is read via ``git show HEAD:<path>``.  Changed
  files are reported via ``git status --porcelain``, which reflects all
  working-tree changes (from any process, not just agent tool calls).  Results
  are not scoped to a session.

- :class:`AgentEditFilesystemRegistry` — used for workspaces that are **not**
  inside a git repository.  Changed files are tracked only when the agent calls
  :meth:`record_change` through a file-write or file-edit tool call.  No
  filesystem-watcher thread is started.  Events are not persisted and are lost
  on server restart.

Use :func:`create_filesystem_registry` to obtain the correct implementation
for a given workspace path.

Both classes share the :class:`FilesystemRegistry` abstract base class, which
defines the full public interface.  The base class also exposes
:attr:`~FilesystemRegistry.tracks_all_changes` /
:attr:`~FilesystemRegistry.tracking_limit_reason` so callers (the
``GET …/changes`` endpoint) can tell the UI when a non-git workspace tracks
only agent tool-call edits — surfacing the limitation instead of letting a
necessarily-partial list read as a definitive "no changes".
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Machine-readable reasons surfaced in the ``GET …/changes`` response's
# ``tracking.reason`` field when change tracking cannot observe every
# working-tree edit.  The web UI maps these to an explanatory notice in the
# Files "changes" panel so a necessarily-partial list does not read as a
# definitive "no changes".
TRACKING_LIMIT_NON_GIT = "non_git_workspace"
TRACKING_LIMIT_NO_WORKSPACE = "no_workspace"

# Filename patterns for ephemeral process artifacts that should never appear in
# the Files panel regardless of .gitignore rules.  These are write-temp files
# produced by editors, package managers, and system tools (not real source
# changes).  Matched against the *filename only* (last path component), not the
# full path.
_EPHEMERAL_PATTERNS: tuple[str, ...] = (
    "*.tmp",  # generic temp files (e.g. pyproject.toml.tmp.12345)
    "*.tmp.*",  # write-then-rename variants with extra suffix
    "*~",  # editor backup files (vim, nano, gedit …)
    "*.swp",  # vim swap files
    "*.swo",  # vim secondary swap files
    "#*#",  # Emacs auto-save files
)

# Directory names to prune when walking the working tree for git-status
# results.  These are build/cache/VCS directories whose contents change
# frequently but are never relevant to the Files panel.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        # Runner-internal directory for terminal session output files.
        # These are never agent-edited source files and must not appear
        # in the Files panel.
        "terminals",
    }
)


def _is_ephemeral(path: str) -> bool:
    """Return ``True`` if the filename matches a known ephemeral artifact pattern.

    Checked against the *filename only* (last path component) so that a temp
    file nested in any subdirectory is still caught.

    :param path: Normalized path (relative or absolute).
    :returns: ``True`` when the filename matches :data:`_EPHEMERAL_PATTERNS`.
    """
    filename = Path(path).name
    return any(fnmatch.fnmatch(filename, pat) for pat in _EPHEMERAL_PATTERNS)


def _net_operation(first: str, last: str) -> str | None:
    """Compute the net filesystem operation from the first and last events seen.

    Uses a two-point state machine rather than a static priority map so that
    sequences like ``deleted → created`` (file replaced within a session) are
    handled correctly.

    Representative sequences:

    ============  ===========  ============  ======================================
    first         last         result        reason
    ============  ===========  ============  ======================================
    ``created``   ``modified`` ``created``   new file, still present
    ``created``   ``deleted``  ``None``      new this session, then removed → hide
    ``modified``  ``deleted``  ``deleted``   pre-existing, now gone
    ``modified``  ``created``  ``modified``  deleted then recreated
    ``deleted``   ``created``  ``modified``  pre-existing file replaced
    ``deleted``   ``modified`` ``modified``  pre-existing file replaced
    ============  ===========  ============  ======================================

    :param first: The operation from the earliest event for a path this session.
    :param last: The operation from the most recent event for the same path.
    :returns: One of ``"created"``, ``"modified"``, ``"deleted"``, or ``None``
        when the file should be hidden entirely (created and deleted this session).
    """
    if first == "created" and last == "deleted":
        return None
    if last == "deleted":
        return "deleted"
    if first == "created":
        return "created"
    return "modified"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _find_git_root(path: Path) -> Path | None:
    """Walk up the directory tree to find the nearest ``.git`` entry.

    Handles both normal clones (``.git/`` directory) and git worktrees
    (``.git`` file, a gitlink pointing at the real git dir).

    :param path: Starting directory (will be resolved to an absolute path).
    :returns: The directory that contains ``.git``, or ``None`` if *path*
        is not inside a git repository.
    """
    current = path.resolve()
    while True:
        git_entry = current / ".git"
        if git_entry.is_dir() or git_entry.is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _normalize_path(path: str, cwd: Path) -> str | None:
    """Return *path* as a workspace-relative string, or ``None`` if it escapes the workspace.

    Resolves both absolute and relative paths against *cwd* (using
    ``Path.resolve(strict=False)`` to handle ``..`` components and symlinks
    without requiring the file to exist).  Paths that resolve outside *cwd*
    are rejected to prevent misleading entries in the Files panel.

    :param path: File path, either absolute or relative to the workspace root.
    :param cwd: Workspace root.  Must already be a fully resolved path (as
        returned by :meth:`pathlib.Path.resolve`).
    :returns: Path relative to the workspace root as a plain string, or
        ``None`` when the path escapes the workspace root.
    """
    p = Path(path)
    resolved = p.resolve(strict=False) if p.is_absolute() else (cwd / p).resolve(strict=False)
    try:
        return str(resolved.relative_to(cwd))
    except ValueError:
        return None


def _unquote_git_path(path: str) -> str:
    """Unescape a git C-quoted path (surrounding double-quotes already stripped).

    Git wraps paths in double-quotes and applies C-style escaping when they
    contain non-printable characters or non-ASCII bytes.  Non-ASCII characters
    appear as UTF-8 octal sequences (e.g. ``é`` → ``\\303\\251``).

    :param path: Raw content between the outer ``"..."`` git-quotes.
    :returns: The decoded path string.
    """
    buf: list[int] = []
    i = 0
    _SIMPLE: dict[str, int] = {
        "\\": ord("\\"),
        '"': ord('"'),
        "n": 0x0A,
        "t": 0x09,
        "r": 0x0D,
        "a": 0x07,
        "b": 0x08,
        "f": 0x0C,
        "v": 0x0B,
    }
    while i < len(path):
        ch = path[i]
        if ch != "\\" or i + 1 >= len(path):
            buf.extend(ch.encode("utf-8"))
            i += 1
            continue
        esc = path[i + 1]
        if esc in _SIMPLE:
            buf.append(_SIMPLE[esc])
            i += 2
        elif (
            esc in "01234567"
            and i + 3 < len(path)
            and path[i + 2] in "01234567"
            and path[i + 3] in "01234567"
        ):
            # Three-digit octal sequence → one raw byte (UTF-8 encoded non-ASCII).
            buf.append(int(path[i + 1 : i + 4], 8))
            i += 4
        else:
            buf.extend(ch.encode("utf-8"))
            i += 1
    return bytes(buf).decode("utf-8", errors="replace")


def _strip_git_quotes(path_part: str) -> str:
    """Strip outer git-quotes and unescape C-escape sequences if present.

    :param path_part: Raw path field from a porcelain line.
    :returns: Unquoted, unescaped path string.
    """
    if path_part.startswith('"') and path_part.endswith('"'):
        return _unquote_git_path(path_part[1:-1])
    return path_part


def _parse_git_porcelain_line(line: str) -> tuple[str, str] | None:
    """Parse one line of ``git status --porcelain`` output.

    Returns ``(git_relative_path, operation)`` where *operation* is one of
    ``"created"``, ``"modified"``, or ``"deleted"``, or ``None`` when the
    line is too short or otherwise malformed.

    Status mapping:

    - ``??`` (untracked) and ``A`` (staged new file) → ``"created"``
    - ``D`` in either column → ``"deleted"``
    - Everything else (``M``, ``R``, ``C``, ``U``, …) → ``"modified"``

    Renames appear as ``R  old -> new``; only the destination path is
    returned.  Git-quoted paths (wrapping double-quotes for names with
    spaces or special characters, including non-ASCII octal sequences) are
    fully unquoted and unescaped via :func:`_unquote_git_path`.

    :param line: A single line from ``git status --porcelain`` output.
    :returns: ``(path, operation)`` tuple or ``None``.
    """
    if len(line) < 4:
        return None
    xy = line[:2]
    path_part = line[3:]

    # Renames/copies: take only the destination path.  Gate on status code so
    # filenames containing literal " -> " are handled correctly.
    if xy[0] in ("R", "C") and " -> " in path_part:
        dest = path_part.split(" -> ", 1)[1]
        path_part = _strip_git_quotes(dest)
    else:
        path_part = _strip_git_quotes(path_part)

    x, y = xy[0], xy[1]
    if (x == "?" and y == "?") or x == "A":
        operation = "created"
    elif x == "D" or y == "D":
        operation = "deleted"
    else:
        operation = "modified"

    return path_part, operation


# ── Data model ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class _FileEvent:
    """A single filesystem event recorded by the agent via a tool call.

    :param path: Normalized file path (relative to cwd when possible).
    :param operation: One of ``"created"``, ``"modified"``, or ``"deleted"``.
    :param timestamp: Unix timestamp (float) when the event was recorded.
    :param bytes: File size in bytes at event time, or ``None`` if stat failed
        or the file was deleted.
    :param modified_at: File modification time as Unix timestamp (int),
        or ``None`` if stat failed or the file was deleted.
    """

    path: str
    operation: str  # "created" | "modified" | "deleted"
    timestamp: float
    bytes: int | None
    modified_at: int | None


# ── Abstract base ─────────────────────────────────────────────────────────────


class FilesystemRegistry(ABC):
    """Abstract base for per-conversation file-change registries.

    Concrete implementations:

    - :class:`GitFilesystemRegistry` — git-backed baseline; reports all working-tree
      changes via ``git status``, regardless of which process wrote them.
    - :class:`AgentEditFilesystemRegistry` — snapshot-backed baseline; tracks only
      files the agent explicitly writes or edits via tool calls; for non-git workspaces.

    Use :func:`create_filesystem_registry` to obtain the correct implementation.
    """

    def __init__(self, watch_path: Path) -> None:
        """Initialize the registry rooted at *watch_path*.

        :param watch_path: The workspace directory to use as root.
        """
        self._cwd = watch_path.resolve()

    # ── Concrete: workspace root ───────────────────────────────────

    @property
    def cwd(self) -> Path:
        """The workspace root directory being watched."""
        return self._cwd

    # ── Concrete: change-tracking completeness ─────────────────────

    @property
    def tracks_all_changes(self) -> bool:
        """Whether the registry observes *every* working-tree change.

        ``True`` means changes are reported regardless of which process made
        them (e.g. :class:`GitFilesystemRegistry`, backed by ``git status``).
        ``False`` (the default) means only edits routed through
        :meth:`record_change` — the agent's write/edit tool calls and the REST
        filesystem endpoints — are visible; files written by a native-harness
        CLI, ``sys_os_shell``, or any external process are missed.  Callers use
        this to tell the UI that the changes list may be incomplete.
        """
        return False

    @property
    def tracking_limit_reason(self) -> str | None:
        """Machine-readable reason change tracking is incomplete.

        ``None`` when :attr:`tracks_all_changes` is ``True``.  Otherwise one of
        the ``TRACKING_LIMIT_*`` constants (e.g. :data:`TRACKING_LIMIT_NON_GIT`),
        surfaced verbatim in the ``GET …/changes`` response so the web UI can
        explain *why* tracking is limited rather than showing a bare empty list.
        """
        return None

    # ── Concrete: record_change (no-op default) ────────────────────

    def record_change(
        self,
        path: str,
        operation: str,
        session_id: str,
    ) -> None:
        """Record a file change made by the agent via a tool call.

        Called by PUT/PATCH file handlers after a successful write or edit.
        The default implementation is a no-op; subclasses override to persist
        the event.

        :param path: Path relative to the workspace root,
            e.g. ``"src/foo.py"``.
        :param operation: One of ``"created"``, ``"modified"``, or
            ``"deleted"``.
        :param session_id: The session that made the change,
            e.g. ``"conv_abc123"``.
        """
        return

    # ── Concrete: snapshot (no-op default) ────────────────────────

    def seed_snapshot(self, path: str, content: str, *, session_id: str | None = None) -> None:
        """Seed a pre-write snapshot for *path*.

        Part of the base interface so callers can call it unconditionally on any
        registry.  No-op by default (e.g. :class:`GitFilesystemRegistry` uses
        ``git show HEAD`` instead of in-memory snapshots).
        :class:`AgentEditFilesystemRegistry` overrides this to store the content
        in memory for use by the diff endpoint.

        :param path: Path relative to the workspace root.
        :param content: File content before the write/edit.
        :param session_id: Optional session scope for the snapshot.
        """
        return

    def unregister_conversation(self, conversation_id: str) -> None:
        """Drop per-session state when a session is deleted.

        Called on session teardown so implementations can evict in-memory
        events and snapshots.  No-op by default (e.g.
        :class:`GitFilesystemRegistry` holds no per-session state).

        :param conversation_id: The conversation to remove,
            e.g. ``"conv_abc123"``.
        """
        return

    def start(self) -> None:
        """Start any background observers.  Idempotent."""
        return

    def stop(self) -> None:
        """Stop any background observers.  Idempotent."""
        return

    # ── Abstract: must be implemented by subclasses ───────────────

    @abstractmethod
    def list_changed_files(self, conversation_id: str, *, limit: int) -> list[dict[str, Any]]:
        """Return changed files visible to *conversation_id*, newest first.

        :param conversation_id: The session to query, e.g. ``"conv_abc123"``.
        :param limit: Maximum number of records to return.
        :returns: List of file-record dicts with ``path``, ``status``,
            ``bytes``, and ``modified_at`` fields, newest first.
        """

    @abstractmethod
    def get_changed_file(self, session_id: str, path: str) -> dict[str, Any] | None:
        """Return the change record for a single *path*, or ``None``.

        :param session_id: The session to query, e.g. ``"conv_abc123"``.
        :param path: Path relative to the workspace root, e.g. ``"src/foo.py"``.
        :returns: A file-record dict with ``path``, ``status``, ``bytes``, and
            ``modified_at`` fields, or ``None`` when the file has no changes.
        """

    @abstractmethod
    def get_baseline(self, path: str) -> str | None:
        """Return the pre-modification baseline content of *path*, or ``None``.

        :param path: Path relative to the workspace root, e.g. ``"src/foo.py"``.
        :returns: File content before modification, or ``None`` when no
            baseline is available (new/untracked file, or no snapshot seeded).
        """


# ── Agent-edit-tracking implementation ───────────────────────────────────────


class AgentEditFilesystemRegistry(FilesystemRegistry):
    """Filesystem registry that tracks only files the agent explicitly modified.

    Changes are recorded via :meth:`record_change`, which is called by the
    file-write (PUT) and file-edit (PATCH) handlers after a successful
    operation.  No filesystem-watcher thread is started — only tool-call
    operations appear in the Files panel.

    Each session maintains its own event list so changes are naturally isolated
    between sessions.  Events are not persisted; they are lost on server restart
    and the Files panel will appear empty after a restart.

    :param watch_path: The directory to track, e.g. ``Path("/home/user/project")``.
    """

    def __init__(self, watch_path: Path) -> None:
        """Initialize the registry rooted at *watch_path*.

        :param watch_path: The workspace directory to use as root.
        """
        super().__init__(watch_path)
        # Per-session ordered event lists: session_id → [_FileEvent, ...]
        self._session_events: dict[str, list[_FileEvent]] = {}
        self._lock = threading.Lock()
        # Per-path snapshots: normalized path → file content captured just
        # before the first write/edit operation on that path this session.
        # Seeded by ``seed_snapshot`` (called from the PUT/PATCH handlers)
        # so the diff endpoint can return the true pre-modification state.
        self._snapshots: dict[str, str] = {}
        self._snapshots_lock = threading.Lock()
        # Per-session snapshot ownership: session_id → set of normalized paths
        # registered by that session.  Used by ``unregister_conversation`` to
        # evict snapshot entries when a session ends, preventing unbounded growth.
        self._snapshot_sessions: dict[str, set[str]] = {}

    @property
    def tracking_limit_reason(self) -> str | None:
        """Non-git workspaces track only agent tool-call edits.

        :returns: :data:`TRACKING_LIMIT_NON_GIT` — external/shell/native-CLI
            writes never reach :meth:`record_change`, so the changes list is
            necessarily partial.  (:attr:`tracks_all_changes` stays ``False``.)
        """
        return TRACKING_LIMIT_NON_GIT

    def record_change(
        self,
        path: str,
        operation: str,
        session_id: str,
    ) -> None:
        """Record a file change made by the agent via a tool call.

        Appends a :class:`_FileEvent` to the session's event list.  The file
        is stat-ed at record time to capture size and mtime; stat errors are
        silently suppressed (e.g. for deleted files).

        Ephemeral process artifacts (see :data:`_EPHEMERAL_PATTERNS`) are
        silently ignored.

        :param path: Path relative to the workspace root,
            e.g. ``"src/foo.py"``.
        :param operation: One of ``"created"``, ``"modified"``, or
            ``"deleted"``.
        :param session_id: The session that made the change,
            e.g. ``"conv_abc123"``.
        """
        norm = _normalize_path(path, self._cwd)
        if norm is None:
            return
        if _is_ephemeral(norm):
            return
        bytes_: int | None = None
        modified_at: int | None = None
        if operation != "deleted":
            norm_path = Path(norm)
            abs_path = norm_path if norm_path.is_absolute() else (self._cwd / norm_path).resolve()
            try:
                st = abs_path.stat()
                bytes_ = st.st_size
                modified_at = int(st.st_mtime)
            except OSError:
                pass
        fe = _FileEvent(
            path=norm,
            operation=operation,
            timestamp=time.time(),
            bytes=bytes_,
            modified_at=modified_at,
        )
        with self._lock:
            self._session_events.setdefault(session_id, []).append(fe)

    def unregister_conversation(self, conversation_id: str) -> None:
        """Drop the event list and evict any snapshot entries for *conversation_id*.

        :param conversation_id: The conversation to remove,
            e.g. ``"conv_abc123"``.
        """
        with self._lock:
            self._session_events.pop(conversation_id, None)
        with self._snapshots_lock:
            paths = self._snapshot_sessions.pop(conversation_id, set())
            for p in paths:
                self._snapshots.pop(p, None)

    def list_changed_files(self, conversation_id: str, *, limit: int) -> list[dict[str, Any]]:
        """Return files changed by the agent in *conversation_id*'s session.

        :param conversation_id: The session to query, e.g.
            ``"conv_abc123"``.
        :param limit: Maximum number of records to return.
        :returns: List of file-record dicts suitable for the
            ``workspace.changed_files`` API response, newest first.
        """
        with self._lock:
            events = list(self._session_events.get(conversation_id, []))
        # Track the first and last operation seen per path so that
        # sequences like deleted→created are resolved correctly.
        first_op: dict[str, str] = {}
        last_op: dict[str, str] = {}
        by_path: dict[str, _FileEvent] = {}
        for e in events:
            # Ephemeral artifacts are filtered here as a second line of
            # defence, primarily for events injected without going through
            # record_change (e.g. in tests).
            if _is_ephemeral(e.path):
                continue
            # Events are appended chronologically, so the last write wins for
            # metadata (bytes, modified_at) without any timestamp comparison.
            if e.path not in first_op:
                first_op[e.path] = e.operation
            last_op[e.path] = e.operation
            by_path[e.path] = e
        # Stamp each retained event with the correct net operation.
        # _net_operation returns None for files created and deleted within the
        # same session; they never existed before and are gone now, so hide them.
        by_path = {
            path: dataclasses.replace(event, operation=op)
            for path, event in by_path.items()
            if (op := _net_operation(first_op[path], last_op[path])) is not None
        }
        records = sorted(
            by_path.values(),
            key=lambda e: (e.modified_at or 0, e.path),
            reverse=True,
        )
        return [
            {
                "path": r.path,
                "status": r.operation,
                "bytes": r.bytes,
                "modified_at": r.modified_at,
            }
            for r in records[:limit]
        ]

    def get_changed_file(self, session_id: str, path: str) -> dict[str, Any] | None:
        """Return the change record for a single *path*, or ``None``.

        Equivalent to scanning :meth:`list_changed_files` for a specific path
        but avoids the 10 000-record cap and the O(N-files) linear scan in the
        caller.  The inner loop is O(E) where E is the total number of events
        for this session — typically much smaller than all changed files.

        :param session_id: The conversation to query, e.g. ``"conv_abc123"``.
        :param path: Path relative to the workspace root, e.g.
            ``"src/foo.py"``.
        :returns: A file-record dict (``path``, ``status``, ``bytes``,
            ``modified_at``) when the file was changed this session, or
            ``None`` when it was not touched.
        """
        norm = _normalize_path(path, self._cwd)
        if norm is None:
            return None
        with self._lock:
            events = [e for e in self._session_events.get(session_id, []) if e.path == norm]
        if not events:
            return None
        first_op = events[0].operation
        last_op = events[-1].operation
        last_event = events[-1]
        op = _net_operation(first_op, last_op)
        if op is None:
            return None
        return {
            "path": norm,
            "status": op,
            "bytes": last_event.bytes,
            "modified_at": last_event.modified_at,
        }

    def get_baseline(self, path: str) -> str | None:
        """Return the pre-modification baseline content of *path*, or ``None``.

        Falls back to the in-memory snapshot captured by :meth:`seed_snapshot`
        before the first write/edit API call.  Returns ``None`` if no snapshot
        was captured (e.g. the file was created new this session).

        :param path: Path relative to the workspace root,
            e.g. ``"src/foo.py"``.
        :returns: File content before it was first modified this session, or
            ``None`` when no baseline is available.
        """
        norm = _normalize_path(path, self._cwd)
        if norm is None:
            return None
        with self._snapshots_lock:
            return self._snapshots.get(norm)

    def seed_snapshot(self, path: str, content: str, *, session_id: str | None = None) -> None:
        """Seed a pre-write snapshot for *path* if one does not already exist.

        Must be called **before** writing new content to the file so the
        snapshot captures the original (pre-modification) state.  If a
        snapshot for *path* already exists this is a no-op.

        :param path: Normalized path relative to the workspace root,
            e.g. ``"src/foo.py"``.
        :param content: Current file content before the upcoming write.
        :param session_id: Optional session identifier.  When provided,
            the path is registered under this session so that
            :meth:`unregister_conversation` can evict it later.
        """
        norm = _normalize_path(path, self._cwd)
        if norm is None:
            return
        with self._snapshots_lock:
            if norm not in self._snapshots:
                self._snapshots[norm] = content
            if session_id:
                self._snapshot_sessions.setdefault(session_id, set()).add(norm)


# ── Git-backed implementation ─────────────────────────────────────────────────


class GitFilesystemRegistry(FilesystemRegistry):
    """Filesystem registry backed by ``git status`` and ``git show``.

    Used when the workspace is inside a git repository.  No background thread
    is started.  :meth:`list_changed_files` and :meth:`get_changed_file`
    always reflect the current working-tree state (staged + unstaged changes
    and untracked files relative to HEAD).  Because git tracks changes from
    HEAD rather than from a point in time, results are not scoped to a
    conversation start time and include changes made by any process (agent
    tool calls, shell commands, external editors, etc.).

    :param watch_path: The workspace directory, e.g.
        ``Path("/home/user/project")``.
    :param git_root: The repository root (directory containing ``.git/``),
        as returned by :func:`_find_git_root`.
    """

    def __init__(self, watch_path: Path, git_root: Path) -> None:
        """Initialize the registry with a git root.

        :param watch_path: The workspace directory.
        :param git_root: The repository root containing ``.git/``.
        """
        super().__init__(watch_path)
        self._git_root = git_root

    @property
    def tracks_all_changes(self) -> bool:
        """``git status`` reflects every working-tree change regardless of which
        process wrote it, so git-backed tracking is complete.

        :returns: ``True``.  (:attr:`tracking_limit_reason` stays ``None``.)
        """
        return True

    def list_changed_files(self, conversation_id: str, *, limit: int) -> list[dict[str, Any]]:
        """Return all uncommitted changes in the working tree, newest first.

        *conversation_id* is accepted for API compatibility but is not used
        to filter results — git status always reflects the current state
        relative to HEAD.

        :param conversation_id: Ignored for git-backed registries.
        :param limit: Maximum number of records to return.
        :returns: List of file-record dicts, newest first.
        """
        try:
            # ``--untracked-files=all`` forces git to expand entirely-untracked
            # directories into their individual files.  Without it, a new file
            # inside a brand-new directory tree collapses to a single ``?? dir/``
            # line, so the UI would show the directory (stat'd as ~96 B) instead
            # of the added file.
            result = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=str(self._git_root),
                capture_output=True,
                timeout=5,
            )
        except Exception:
            _logger.debug(
                "GitFilesystemRegistry.list_changed_files: git status failed", exc_info=True
            )
            return []

        if result.returncode != 0:
            return []

        records: list[dict[str, Any]] = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            parsed = _parse_git_porcelain_line(line)
            if parsed is None:
                continue
            git_path, operation = parsed
            rel_path = self._git_to_rel(git_path)
            if rel_path is None:
                continue
            # Skip runner-internal and build directories (e.g. terminals/,
            # node_modules/).  These are never agent-edited source files.
            first_component = Path(rel_path).parts[0] if Path(rel_path).parts else ""
            if first_component in _SKIP_DIRS:
                continue
            records.append(self._make_record(rel_path, operation))

        records.sort(key=lambda r: (r["modified_at"] or 0, r["path"]), reverse=True)
        return records[:limit]

    def get_changed_file(self, session_id: str, path: str) -> dict[str, Any] | None:
        """Return the change record for a single *path*, or ``None``.

        Queries ``git status --porcelain -- <path>`` for the specific file
        rather than scanning the full working-tree diff.

        :param session_id: Ignored for git-backed registries.
        :param path: Path relative to the workspace root.
        :returns: A file-record dict or ``None`` when the file has no
            uncommitted changes.
        """
        norm = _normalize_path(path, self._cwd)
        if norm is None:
            return None
        try:
            cwd_prefix = self._cwd.relative_to(self._git_root)
            git_path = (cwd_prefix / norm).as_posix()
        except ValueError:
            return None

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--", git_path],
                cwd=str(self._git_root),
                capture_output=True,
                timeout=5,
            )
        except Exception:
            _logger.debug(
                "GitFilesystemRegistry.get_changed_file: git status failed", exc_info=True
            )
            return None

        if result.returncode != 0:
            return None

        output = result.stdout.decode("utf-8", errors="replace")
        for line in output.splitlines():
            parsed = _parse_git_porcelain_line(line)
            if parsed is None:
                continue
            _, operation = parsed
            return self._make_record(norm, operation)

        return None

    def get_baseline(self, path: str) -> str | None:
        """Return committed content via ``git show HEAD:<path>``.

        :param path: Path relative to the workspace root.
        :returns: Content of the file at HEAD, or ``None`` for new/untracked
            files or when the subprocess fails.
        """
        norm = _normalize_path(path, self._cwd)
        if norm is None:
            return None
        try:
            cwd_prefix = self._cwd.relative_to(self._git_root)
            git_path = (cwd_prefix / norm).as_posix()
        except ValueError:
            return None

        try:
            result = subprocess.run(
                ["git", "show", f"HEAD:{git_path}"],
                cwd=str(self._git_root),
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            _logger.debug(
                "GitFilesystemRegistry.get_baseline: git show failed for %r",
                norm,
                exc_info=True,
            )
        return None

    # ── Internals ─────────────────────────────────────────────────

    def _git_to_rel(self, git_path: str) -> str | None:
        """Convert a git-root-relative path to a cwd-relative path.

        :param git_path: Path relative to ``self._git_root``.
        :returns: Path relative to ``self._cwd``, or ``None`` if the path
            is not under ``self._cwd``.
        """
        abs_path = self._git_root / git_path
        try:
            return str(abs_path.relative_to(self._cwd))
        except ValueError:
            return None

    def _make_record(self, rel_path: str, operation: str) -> dict[str, Any]:
        """Build a file-record dict for *rel_path*.

        :param rel_path: Path relative to ``self._cwd``.
        :param operation: One of ``"created"``, ``"modified"``, ``"deleted"``.
        :returns: File-record dict with ``path``, ``status``, ``bytes``, and
            ``modified_at`` fields.
        """
        bytes_: int | None = None
        modified_at: int | None = None
        if operation != "deleted":
            try:
                st = (self._cwd / rel_path).stat()
                bytes_ = st.st_size
                modified_at = int(st.st_mtime)
            except OSError:
                pass
        return {"path": rel_path, "status": operation, "bytes": bytes_, "modified_at": modified_at}


# ── Factory ───────────────────────────────────────────────────────────────────


def create_filesystem_registry(watch_path: Path) -> FilesystemRegistry:
    """Return the appropriate :class:`FilesystemRegistry` for *watch_path*.

    Detects whether *watch_path* is inside a git repository and returns:

    - :class:`GitFilesystemRegistry` when a ``.git`` entry is found at or
      above *watch_path*.
    - :class:`AgentEditFilesystemRegistry` otherwise.

    :param watch_path: The workspace root to track.
    :returns: A :class:`FilesystemRegistry` instance ready to be used.
    """
    git_root = _find_git_root(watch_path.resolve())
    if git_root is not None:
        return GitFilesystemRegistry(watch_path, git_root)
    return AgentEditFilesystemRegistry(watch_path)
