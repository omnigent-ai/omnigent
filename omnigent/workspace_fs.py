"""Process-agnostic, read-only workspace filesystem reader.

The runner serves the web UI's file panel (directory browse, changed
files, diffs, search, file content) by reading its sandboxed
workspace.  When the runner process dies but the host that holds the
workspace on disk is still connected, the host serves the same panel by
running this module against the workspace directory directly.

This reader is deliberately *read-only* and *sandbox-free*: it never
writes, never runs a shell, and confines every path to the workspace
root.  It reuses the runner's pure helpers (glob translation, path
validation, pagination, the git/edit change registry) so the JSON it
returns is byte-identical to the runner's filesystem endpoints — the
server proxy layer and the frontend cannot tell which side answered.

The returned dicts match, one-to-one, the runner endpoints in
``omnigent/runner/app.py``:

- :meth:`WorkspaceReader.list_or_read` → ``_fs_list_or_read``
- :meth:`WorkspaceReader.changes`      → ``list_filesystem_changes``
- :meth:`WorkspaceReader.diff`         → ``read_environment_file_diff``
- :meth:`WorkspaceReader.search`       → ``search_environment_files``

Change-tracking caveat: in a **git** workspace the changed-files list
and diff baselines come from ``git status`` / ``git show`` and are fully
reconstructable from disk, so the host serves them exactly like the
runner.  In a **non-git** workspace the runner tracks changes from the
live agent's tool calls (in-memory), which the host does not have — so
the host returns an empty changed-files list there.  Directory browse,
search, and file content work identically in both modes.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from omnigent.entities.environment_filesystem import InvalidPath
from omnigent.entities.pagination import paginate_in_memory
from omnigent.inner.os_env import _DEFAULT_READ_LIMIT
from omnigent.runner.environment_filesystem import (
    _glob_to_regex,
    _validate_path,
    split_glob_list,
)
from omnigent.runtime.filesystem_registry import (
    GitStatusUnavailable,
    create_filesystem_registry,
)

# Match the runner's caps so a host-served read is truncated identically.
_MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MiB


class WorkspaceReaderError(Exception):
    """A workspace read failed with a specific HTTP-mappable outcome.

    Carries a ``status`` code and an error ``code``/``message`` so the
    host handler can echo the same shape the runner endpoints return
    (404 not-found, 400 invalid-path, 500 git-status-failed).

    :param status: HTTP status the runner would have returned.
    :param code: Machine-readable error code, e.g. ``"not_found"``.
    :param message: Human-readable detail.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class WorkspaceReader:
    """Read-only view of a workspace directory, confined to its root.

    :param root: Absolute path to the workspace directory on disk, e.g.
        ``Path("/Users/alice/project")``.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        # The change registry (git or edit-tracking) is chosen the same
        # way the runner chooses it, so git workspaces get git-status
        # semantics and everything else degrades to an empty list.
        self._registry = create_filesystem_registry(self._root)

    # ── Path confinement ──────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        """Resolve a relative path to an absolute path under the root.

        :param path: Relative path within the workspace (``""`` = root).
        :returns: Resolved absolute path guaranteed under the root.
        :raises WorkspaceReaderError: 400 when the path escapes the root
            or is otherwise invalid.
        """
        try:
            validated = _validate_path(path) if path else ""
        except InvalidPath as exc:
            raise WorkspaceReaderError(400, "invalid_path", str(exc)) from exc
        if not validated:
            return self._root
        full = (self._root / validated).resolve()
        try:
            full.relative_to(self._root)
        except ValueError as exc:
            raise WorkspaceReaderError(
                400, "invalid_path", f"Path {path!r} escapes the workspace root"
            ) from exc
        return full

    # ── Directory listing / file content ──────────────────────────

    def list_or_read(
        self,
        path: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> dict[str, Any]:
        """List a directory or read a file, mirroring ``_fs_list_or_read``.

        :param path: Relative path (``""`` for the workspace root).
        :param limit: Max entries for a directory listing.
        :param after: Forward-pagination cursor entry id.
        :param before: Backward-pagination cursor entry id.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: A directory-listing dict or a file-content dict.
        :raises WorkspaceReaderError: On invalid path or missing file.
        """
        resolved = self._resolve(path)
        if resolved.is_dir():
            return self._list_dir(path, resolved, limit, after, before, order)
        return self._read_file(path, resolved)

    def _list_dir(
        self,
        rel: str,
        resolved: Path,
        limit: int,
        after: str | None,
        before: str | None,
        order: str,
    ) -> dict[str, Any]:
        """Build the directory-listing payload for a resolved directory.

        Classifies entries by target type (follows symlinks) and skips
        per-entry ``OSError`` (e.g. a broken symlink) so one bad entry
        does not fail the listing — matching the runner's ``list_dir``.
        """
        validated = _validate_path(rel) if rel else ""
        entries: list[dict[str, Any]] = []
        try:
            names = sorted(os.listdir(resolved))
        except OSError as exc:
            raise WorkspaceReaderError(
                404, "not_found", f"Directory {rel!r} not found or not accessible"
            ) from exc
        for name in names:
            full = resolved / name
            child_rel = os.path.join(validated, name) if validated else name
            try:
                st = full.stat()  # follows symlinks, like the runner
                is_dir = full.is_dir()
                entry_type = "directory" if is_dir else "file"
                size = st.st_size if entry_type == "file" else None
                mtime = int(st.st_mtime)
            except OSError:
                # Broken symlink (target gone): fall back to lstat and list it
                # as a file with no size, matching the runner's list_dir rather
                # than dropping the entry.
                try:
                    ls = full.lstat()
                except OSError:
                    continue
                entry_type = "file"
                size = None
                mtime = int(ls.st_mtime)
            entries.append(
                {
                    "id": child_rel,
                    "object": "session.environment.filesystem.entry",
                    "name": name,
                    "path": child_rel,
                    "type": entry_type,
                    "bytes": size,
                    "modified_at": mtime,
                }
            )
        page = paginate_in_memory(
            entries,
            id_fn=lambda e: e["id"],
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        return {
            "object": "list",
            "data": page.data,
            "first_id": page.first_id,
            "last_id": page.last_id,
            "has_more": page.has_more,
        }

    def _read_file(
        self,
        rel: str,
        resolved: Path,
        *,
        limit: int | None = _DEFAULT_READ_LIMIT,
    ) -> dict[str, Any]:
        """Build the file-content payload for a resolved file.

        Text files are UTF-8 decoded and line-capped at ``limit``; binary
        files are base64-encoded.  Both are byte-capped at
        :data:`_MAX_READ_BYTES`.  Shape matches the runner's file-content
        response, including the mimetype guess.

        Reads at most ``_MAX_READ_BYTES`` from disk (like the runner's
        bounded read) rather than slurping the whole file, so opening a
        multi-GB file in the viewer can't OOM the host process.
        """
        try:
            with resolved.open("rb") as fh:
                # One extra byte lets us detect (and flag) truncation
                # without loading the rest of a large file into memory.
                capped = fh.read(_MAX_READ_BYTES + 1)
        except OSError as exc:
            raise WorkspaceReaderError(404, "not_found", f"Path {rel!r} not found") from exc

        return self._file_content_payload(rel, capped, limit=limit)

    def _file_content_payload(
        self,
        rel: str,
        raw: bytes,
        *,
        limit: int | None,
    ) -> dict[str, Any]:
        """Assemble the file-content dict from raw bytes."""
        content_type_guess, _ = mimetypes.guess_type(rel)
        truncated = False
        capped = raw
        if len(capped) > _MAX_READ_BYTES:
            capped = capped[:_MAX_READ_BYTES]
            truncated = True

        try:
            text = capped.decode("utf-8")
            is_text = True
        except UnicodeDecodeError as exc:
            # A byte-cap truncation can split a multi-byte codepoint at the very
            # end, which would otherwise flip an oversize *text* file to base64.
            # When the only invalid bytes are a partial trailing codepoint (the
            # error starts within the last 3 bytes of the truncated buffer),
            # drop them and retry — matching the runner's boundary-safe
            # truncation so the same file serves as text from either side. A
            # genuinely binary file has invalid bytes earlier in the buffer, so
            # this guard doesn't rescue it and it falls through to base64.
            if truncated and exc.start >= len(capped) - 3:
                capped = capped[: exc.start]
                text = capped.decode("utf-8")
                is_text = True
            else:
                is_text = False

        payload: dict[str, Any] = {
            "object": "session.environment.filesystem.file_content",
            "path": rel,
            "content_type": content_type_guess,
        }
        if is_text:
            if limit is not None:
                lines = text.splitlines(keepends=True)
                if len(lines) > limit:
                    text = "".join(lines[:limit])
                    truncated = True
            data = text.encode("utf-8")
            payload["bytes"] = len(data)
            payload["truncated"] = truncated
            payload["encoding"] = "utf-8"
            payload["content"] = text
        else:
            payload["bytes"] = len(capped)
            payload["truncated"] = truncated
            payload["encoding"] = "base64"
            payload["content"] = base64.b64encode(capped).decode()
        return payload

    # ── Search ─────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        include: str | None = None,
        exclude: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Search files by substring + glob filters, like the runner.

        :param query: Case-insensitive substring matched against name and
            relative path.  Whitespace-only yields an empty result.
        :param include: Comma-separated include globs (VSCode/Cursor
            subset), e.g. ``"*.ts,src/**"``.
        :param exclude: Comma-separated exclude globs.
        :param limit: Maximum results (capped at 500 by the caller).
        :returns: A list payload of matching file entries.
        """
        q = query.strip().lower()
        if not q:
            return {"object": "list", "data": [], "has_more": False}

        inc = [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in split_glob_list(include)]
        exc = [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in split_glob_list(exclude)]

        results: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            rel_dir = os.path.relpath(dirpath, self._root)
            # Prune excluded subtrees so a "**/node_modules" pattern
            # avoids descending, matching the runner's search walk.
            kept = []
            for d in sorted(dirnames):
                dp = os.path.normpath(os.path.join("" if rel_dir == "." else rel_dir, d))
                if any(r.match(dp) for r in exc):
                    continue
                kept.append(d)
            dirnames[:] = kept
            for fname in sorted(filenames):
                p = os.path.normpath(os.path.join("" if rel_dir == "." else rel_dir, fname))
                if exc and any(r.match(p) for r in exc):
                    continue
                if inc and not any(r.match(p) for r in inc):
                    continue
                if q not in fname.lower() and q not in p.lower():
                    continue
                try:
                    st = (Path(dirpath) / fname).stat()
                    size: int | None = st.st_size
                    mtime: int | None = int(st.st_mtime)
                except OSError:
                    size = None
                    mtime = None
                results.append(
                    {
                        "id": p,
                        "object": "session.environment.filesystem.entry",
                        "name": fname,
                        "path": p,
                        "type": "file",
                        "bytes": size,
                        "modified_at": mtime,
                    }
                )
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        results.sort(key=lambda e: e["path"])
        return {"object": "list", "data": results, "has_more": len(results) >= limit}

    # ── Changed files / diff ───────────────────────────────────────

    def changes(self, session_id: str) -> dict[str, Any]:
        """List changed files, mirroring ``list_filesystem_changes``.

        Git workspaces report the working-tree diff (``git status``);
        non-git workspaces report an empty list because the host has no
        access to the live agent's in-memory edit history.

        :param session_id: Session id (used only by the edit-tracking
            registry; ignored in git mode).
        :returns: A list payload of changed-file entries.
        :raises WorkspaceReaderError: 500 when ``git status`` fails.
        """
        try:
            raw_changes = self._registry.list_changed_files(session_id, limit=10_000)
        except GitStatusUnavailable as exc:
            raise WorkspaceReaderError(500, "git_status_failed", exc.reason) from exc
        data = [
            {
                "object": "session.environment.filesystem.entry",
                "path": rec["path"],
                "name": rec["path"].split("/")[-1],
                "status": rec["status"],
                "bytes": rec.get("bytes"),
                "modified_at": rec.get("modified_at"),
                "lines_added": rec.get("lines_added"),
                "lines_removed": rec.get("lines_removed"),
            }
            for rec in raw_changes
        ]
        return {"object": "list", "data": data, "has_more": False}

    def diff(self, session_id: str, relative_path: str) -> dict[str, Any]:
        """Return before/after content, mirroring the runner diff endpoint.

        :param session_id: Session id (git mode ignores it).
        :param relative_path: Path relative to the workspace root.
        :returns: A file-diff dict with ``before``/``after`` strings.
        :raises WorkspaceReaderError: On invalid path, git failure, or a
            path not in the changed-files registry (404).
        """
        try:
            relative_path = _validate_path(relative_path)
        except InvalidPath as exc:
            raise WorkspaceReaderError(400, "invalid_path", str(exc)) from exc
        if not relative_path:
            raise WorkspaceReaderError(400, "invalid_path", "Cannot diff the workspace root")

        try:
            record = self._registry.get_changed_file(session_id, relative_path)
        except GitStatusUnavailable as exc:
            raise WorkspaceReaderError(500, "git_status_failed", exc.reason) from exc
        if record is None:
            raise WorkspaceReaderError(
                404,
                "not_found",
                f"Path {relative_path!r} is not in the changed-files registry for this session",
            )

        is_deleted = record.get("status") == "deleted"
        before: str | None = self._registry.get_baseline(relative_path)
        after: str | None = None
        if not is_deleted:
            resolved = self._resolve(relative_path)
            try:
                # Bounded read (like _read_file) so a huge changed file can't
                # OOM the host; the diff view caps at _MAX_READ_BYTES anyway.
                with resolved.open("rb") as fh:
                    raw = fh.read(_MAX_READ_BYTES)
                after = raw.decode("utf-8", errors="replace")
            except OSError:
                after = None
        return {
            "object": "session.environment.filesystem.file_diff",
            "path": relative_path,
            "before": before,
            "after": after,
        }
