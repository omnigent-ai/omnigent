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
import bisect
import mimetypes
import os
import re
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypeAlias, TypedDict, cast

from omnigent.entities.environment_filesystem import FilesystemEntry, InvalidPath
from omnigent.entities.pagination import PagedList, paginate_in_memory
from omnigent.runner import pr_resource
from omnigent.runner.environment_filesystem import (
    _SEARCH_SCAN_BUDGET,
    _glob_to_regex,
    _validate_path,
    entry_payload,
    index_search,
    merge_entries,
    split_glob_list,
)
from omnigent.runtime.filesystem_registry import (
    FilesystemRegistry,
    GitStatusUnavailable,
    create_filesystem_registry,
)
from omnigent.workspace_file_search import walk_search

# Match the runner's caps so a host-served read is truncated identically.
_MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MiB
# Longer than any platform's PATH_MAX, so a real link always fits.
_MAX_LINK_TEXT_BYTES = 4096
_REPOSITORY_BRANCH_TIMEOUT_S = 3.0
_MAX_REPOSITORY_BRANCHES = 10
# Chains longer than this stop early; the client lists the rest on expand.
_MAX_DIRECTORY_CHAIN = 64

_WorkspacePayload: TypeAlias = dict[str, object]


class ListedEntry(TypedDict):
    """One directory entry, in the runner's listing shape."""

    id: str
    object: str
    name: str
    path: str
    type: str
    bytes: int | None
    modified_at: int
    # The deepest directory of a single-child chain, when the listing was asked to collapse them.
    leaf_directory_path: NotRequired[str]


def _repository_branch(url: str, checkout: Path) -> _WorkspacePayload:
    """Inspect one checkout without fetching or trusting stale remote-tracking refs."""
    result: _WorkspacePayload = {"url": url, "branch": None, "pushed": None}
    if not (checkout / ".git").exists():
        return result

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=checkout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            capture_output=True,
            text=True,
            timeout=_REPOSITORY_BRANCH_TIMEOUT_S,
            check=False,
        )

    try:
        current = git("symbolic-ref", "--quiet", "HEAD")
        current_ref = current.stdout.strip()
        if current.returncode or not current_ref.startswith("refs/heads/"):
            return result
        branch = current_ref.removeprefix("refs/heads/")
        result["branch"] = branch
        remote = git("ls-remote", "--exit-code", "--heads", "--", url, f"refs/heads/{branch}")
        if remote.returncode == 2:
            result["pushed"] = False
        elif remote.returncode == 0:
            result["pushed"] = any(
                line.partition("\t")[2] == f"refs/heads/{branch}"
                for line in remote.stdout.splitlines()
            )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


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


@dataclass(frozen=True)
class FileBytes:
    """A file's bytes, capped at the read limit, and how to present them.

    :param data: The bytes to serve, at most ``_MAX_READ_BYTES``.
    :param truncated: Whether the file was longer than the cap.
    :param is_text: Whether ``data`` is valid UTF-8.
    :param content_type: MIME type guessed from the name, e.g. ``"text/markdown"``.
    """

    data: bytes
    truncated: bool
    is_text: bool
    content_type: str | None


def guess_content_type(rel: str) -> str | None:
    """The MIME type a file read reports for ``rel``, from its name alone.

    Uses this machine's MIME table, e.g. ``"text/markdown"`` for ``"notes.md"``.
    """
    content_type, _ = mimetypes.guess_type(rel)
    return content_type


def _cap_file_bytes(rel: str, raw: bytes) -> FileBytes:
    """Cap ``raw`` at the read limit and decide whether it is UTF-8 text."""
    content_type = guess_content_type(rel)
    truncated = len(raw) > _MAX_READ_BYTES
    capped = raw[:_MAX_READ_BYTES] if truncated else raw
    try:
        capped.decode("utf-8")
    except UnicodeDecodeError as exc:
        # The cap may split a trailing codepoint: drop that partial tail so oversize
        # text stays text, as the runner does. Earlier invalid bytes mean binary.
        if not (truncated and exc.start >= len(capped) - 3):
            return FileBytes(capped, truncated, is_text=False, content_type=content_type)
        capped = capped[: exc.start]
    return FileBytes(capped, truncated, is_text=True, content_type=content_type)


def is_utf8_encodable(text: str) -> bool:
    """Return whether ``text`` has no surrogate-escaped bytes, so it encodes as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def single_child_chain(directory: Path) -> list[str]:
    """Follow ``directory`` while its only entry is a real (non-symlink) directory.

    Stops at a second entry, a file, a symlink, a name that isn't UTF-8, an
    unreadable directory, or :data:`_MAX_DIRECTORY_CHAIN` levels.

    :param directory: The directory to start from, e.g. ``root / "com"``.
    :returns: The names traversed, e.g. ``["example", "app"]``; empty when
        ``directory`` doesn't hold exactly one directory.
    """
    names: list[str] = []
    current = directory
    while len(names) < _MAX_DIRECTORY_CHAIN:
        try:
            with os.scandir(current) as entries:
                first = next(entries, None)
                if first is None or next(entries, None) is not None:
                    break
                if not is_utf8_encodable(first.name) or not first.is_dir(follow_symlinks=False):
                    break
        except OSError:
            break
        names.append(first.name)
        current = current / first.name
    return names


def _resolve_workspace_path(root: Path, path: str) -> Path:
    """Resolve a relative path, including symlinks, within the workspace root."""
    try:
        validated = _validate_path(path) if path else ""
    except InvalidPath as exc:
        raise WorkspaceReaderError(400, "invalid_path", str(exc)) from exc
    if not validated:
        return root
    full = (root / validated).resolve()
    try:
        full.relative_to(root)
    except ValueError as exc:
        raise WorkspaceReaderError(
            400, "invalid_path", f"Path {path!r} escapes the workspace root"
        ) from exc
    return full


def read_repository_branches(root: Path, repositories: list[dict[str, str]]) -> _WorkspacePayload:
    """Read current branches and their availability at the original clone URLs.

    Targets come from server-owned launch metadata. Paths remain confined
    to this workspace; failures and detached HEADs return unknown state.
    """
    if len(repositories) > _MAX_REPOSITORY_BRANCHES:
        raise WorkspaceReaderError(400, "invalid_repositories", "Too many repositories")
    root = root.resolve()
    targets = [
        (repository["url"], _resolve_workspace_path(root, repository["path"]))
        for repository in repositories
    ]
    if not targets:
        return {"repositories": []}
    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        branches = list(executor.map(lambda target: _repository_branch(*target), targets))
    return {"repositories": branches}


def _describe_entry(full: Path, name: str, rel: str) -> ListedEntry | None:
    """Describe one directory entry as the runner's ``list_dir`` does.

    Classifies it by its target's type (following a symlink) and lists a
    broken symlink as a file with no size, rather than dropping it.

    :returns: The entry, or ``None`` when even ``lstat`` fails.
    """
    try:
        st = full.stat()  # follows symlinks, like the runner
        entry_type = "directory" if stat.S_ISDIR(st.st_mode) else "file"
        size = st.st_size if entry_type == "file" else None
        mtime = int(st.st_mtime)
    except OSError:
        try:
            ls = full.lstat()
        except OSError:
            return None
        entry_type = "file"
        size = None
        mtime = int(ls.st_mtime)
    return {
        "id": rel,
        "object": "session.environment.filesystem.entry",
        "name": name,
        "path": rel,
        "type": entry_type,
        "bytes": size,
        "modified_at": mtime,
    }


class WorkspaceReader:
    """Read-only view of a workspace directory, confined to its root.

    :param root: Absolute path to the workspace directory on disk, e.g.
        ``Path("/Users/alice/project")``.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._started_registry: FilesystemRegistry | None = None
        self._registry_lock = threading.Lock()

    @property
    def _registry(self) -> FilesystemRegistry:
        """The change registry, started on first use by changes, diffs or search.

        Chosen the way the runner chooses it, so git workspaces get git-status
        semantics and everything else degrades to an empty list. Starting it
        may write the repository's config, so readers that only list or read
        files never start it.
        """
        with self._registry_lock:
            if self._started_registry is None:
                self._started_registry = create_filesystem_registry(self._root)
                self._started_registry.start()
            return self._started_registry

    @property
    def root(self) -> Path:
        """The workspace directory, resolved."""
        return self._root

    # ── Path confinement ──────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        """Resolve a relative path to an absolute path under the root.

        :param path: Relative path within the workspace (``""`` = root).
        :returns: Resolved absolute path guaranteed under the root.
        :raises WorkspaceReaderError: 400 when the path escapes the root
            or is otherwise invalid.
        """
        return _resolve_workspace_path(self._root, path)

    def resolve_directory(self, path: str) -> Path:
        """The confined directory :meth:`list_directory` reads for ``path``, symlinks resolved.

        :param path: Relative directory path (``""`` for the workspace root).
        :raises WorkspaceReaderError: 400 when the path escapes the root or is invalid.
        """
        return self._resolve(path)

    # ── Directory listing / file content ──────────────────────────

    def list_or_read(
        self,
        path: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> _WorkspacePayload:
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

    def list_directory(
        self,
        path: str,
        *,
        limit: int,
        after: str | None,
        collapse_single_child_directories: bool = False,
    ) -> PagedList[ListedEntry]:
        """List a directory in ascending name order, like ``list_or_read``.

        :param path: Relative directory path (``""`` for the workspace root).
        :param limit: Max entries to return.
        :param after: Return entries after this entry id. An id whose entry
            was removed since still resumes at its place in name order.
        :param collapse_single_child_directories: Give each real (non-symlink)
            directory whose only entry is a directory a ``leaf_directory_path``,
            the deepest directory of that chain (see :func:`single_child_chain`).
        :returns: The page of entries.
        :raises WorkspaceReaderError: 404 when ``path`` is not a readable directory.
        """
        resolved = self._resolve(path)
        entries = self._list_entries(path, resolved)
        # Ids sort like names (one shared parent), so the cursor needn't still exist.
        start = bisect.bisect_right([e["id"] for e in entries], after) if after else 0
        page = entries[start : start + limit]
        if collapse_single_child_directories:
            for entry in page:
                child = resolved / entry["name"]
                if entry["type"] != "directory" or child.is_symlink():
                    continue
                chain = single_child_chain(child)
                if chain:
                    entry["leaf_directory_path"] = "/".join([entry["path"], *chain])
        return PagedList(
            data=page,
            first_id=page[0]["id"] if page else None,
            last_id=page[-1]["id"] if page else None,
            has_more=start + limit < len(entries),
        )

    def read_file_bytes(self, path: str) -> FileBytes:
        """Read a file's capped bytes, exactly as ``list_or_read`` would serve them.

        :param path: Relative file path.
        :returns: The capped bytes and their presentation.
        :raises WorkspaceReaderError: 400 on an invalid path, 404 when ``path``
            is not a readable regular file.
        """
        return _cap_file_bytes(path, self._read_capped(path, self._resolve(path)))

    def stat_entry(self, path: str) -> tuple[ListedEntry, bool]:
        """Describe one path exactly as listing its parent would.

        The parent must stay inside the workspace; the leaf itself may be a
        symlink, even one pointing outside, as a listing shows it.

        :param path: Relative path (``""`` for the workspace root).
        :returns: The entry, and whether the path itself is a symlink.
        :raises WorkspaceReaderError: 400 on an invalid path or escaping
            parent, 404 when the path doesn't exist.
        """
        leaf, parent = self._leaf_beneath(path)
        if parent is None:
            entry = _describe_entry(self._root, self._root.name, "")
            if entry is None:
                raise WorkspaceReaderError(404, "not_found", "The workspace is not accessible")
            return entry, False
        full = parent / leaf.rsplit("/", 1)[-1]
        entry = _describe_entry(full, full.name, leaf)
        if entry is None or not is_utf8_encodable(full.name):
            raise WorkspaceReaderError(404, "not_found", f"Path {path!r} not found")
        return entry, full.is_symlink()

    def read_link_text(self, path: str) -> bytes:
        """Return a symlink's own text, without following it.

        The link may point anywhere, even outside the workspace or nowhere;
        only its parent must stay inside the workspace.

        :param path: Relative path of the link.
        :returns: The stored target, at most :data:`_MAX_LINK_TEXT_BYTES`.
        :raises WorkspaceReaderError: 400 on an invalid path, escaping parent
            or oversized link, 404 when missing, 412 when not a symlink.
        """
        leaf, parent = self._leaf_beneath(path)
        if parent is None:
            raise WorkspaceReaderError(412, "not_a_symlink", "The workspace root is not a link")
        full = parent / leaf.rsplit("/", 1)[-1]
        try:
            st = full.lstat()
        except OSError as exc:
            raise WorkspaceReaderError(404, "not_found", f"Path {path!r} not found") from exc
        if not stat.S_ISLNK(st.st_mode):
            raise WorkspaceReaderError(412, "not_a_symlink", f"Path {path!r} is not a symlink")
        try:
            target = os.readlink(os.fsencode(full))
        except OSError as exc:
            raise WorkspaceReaderError(404, "not_found", f"Path {path!r} not found") from exc
        if len(target) > _MAX_LINK_TEXT_BYTES:
            raise WorkspaceReaderError(400, "link_too_long", f"Link {path!r} is too long")
        return target

    def _leaf_beneath(self, path: str) -> tuple[str, Path | None]:
        """Validate *path* and resolve its parent inside the workspace.

        :returns: The validated path, and its resolved parent, or ``None``
            for the workspace root.
        """
        try:
            leaf = _validate_path(path) if path else ""
        except InvalidPath as exc:
            raise WorkspaceReaderError(400, "invalid_path", str(exc)) from exc
        if not leaf:
            return "", None
        parent_rel = leaf.rsplit("/", 1)[0] if "/" in leaf else ""
        return leaf, self._resolve(parent_rel)

    def _list_dir(
        self,
        rel: str,
        resolved: Path,
        limit: int,
        after: str | None,
        before: str | None,
        order: str,
    ) -> _WorkspacePayload:
        """Build the directory-listing payload for a resolved directory."""
        page = paginate_in_memory(
            self._list_entries(rel, resolved),
            id_fn=lambda entry: entry["id"],
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

    def _list_entries(self, rel: str, resolved: Path) -> list[ListedEntry]:
        """List a resolved directory's entries in name order.

        Classifies entries by target type (follows symlinks) and skips
        per-entry ``OSError`` (e.g. a broken symlink) so one bad entry
        does not fail the listing — matching the runner's ``list_dir``.
        """
        validated = _validate_path(rel) if rel else ""
        entries: list[ListedEntry] = []
        try:
            names = sorted(os.listdir(resolved))
        except OSError as exc:
            raise WorkspaceReaderError(
                404, "not_found", f"Directory {rel!r} not found or not accessible"
            ) from exc
        for name in names:
            if not is_utf8_encodable(name):
                # A non-UTF-8 name comes back surrogate-escaped; no JSON or protobuf
                # answer can carry it, so skip it rather than fail the listing.
                continue
            child_rel = os.path.join(validated, name) if validated else name
            entry = _describe_entry(resolved / name, name, child_rel)
            if entry is not None:
                entries.append(entry)
        return entries

    def _read_file(
        self,
        rel: str,
        resolved: Path,
    ) -> _WorkspacePayload:
        """Build the file-content payload for a resolved file.

        Text files are UTF-8 decoded; binary files are base64-encoded.
        Both are byte-capped at :data:`_MAX_READ_BYTES`.
        Shape matches the runner's file-content
        response, including the mimetype guess.

        Reads at most ``_MAX_READ_BYTES + 1`` from disk, bounding memory
        use before decoding oversized files.
        """
        return self._file_content_payload(rel, self._read_capped(rel, resolved))

    def _read_capped(self, rel: str, resolved: Path) -> bytes:
        """Read up to the cap plus one byte of a regular file.

        Opens without blocking and refuses anything but a regular file, so a
        FIFO or device in the workspace answers "not found" instead of hanging.
        """
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(resolved, flags)
        except OSError as exc:
            raise WorkspaceReaderError(404, "not_found", f"Path {rel!r} not found") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkspaceReaderError(404, "not_found", f"Path {rel!r} not found")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                # One extra byte lets us detect (and flag) truncation
                # without loading the rest of a large file into memory.
                return fh.read(_MAX_READ_BYTES + 1)
        except OSError as exc:
            raise WorkspaceReaderError(404, "not_found", f"Path {rel!r} not found") from exc
        finally:
            os.close(fd)

    def _file_content_payload(
        self,
        rel: str,
        raw: bytes,
    ) -> _WorkspacePayload:
        """Assemble the file-content dict from raw bytes."""
        file = _cap_file_bytes(rel, raw)
        return {
            "object": "session.environment.filesystem.file_content",
            "path": rel,
            "content_type": file.content_type,
            "bytes": len(file.data),
            "truncated": file.truncated,
            "encoding": "utf-8" if file.is_text else "base64",
            "content": file.data.decode("utf-8")
            if file.is_text
            else base64.b64encode(file.data).decode(),
        }

    # ── Search ─────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        include: str | None = None,
        exclude: str | None = None,
        limit: int = 500,
    ) -> _WorkspacePayload:
        """Search files by substring + glob filters, like the runner.

        :param query: Case-insensitive substring matched against name and
            relative path.  Whitespace-only yields an empty result.
        :param include: Comma-separated include globs (VSCode/Cursor
            subset), e.g. ``"*.ts,src/**"``.
        :param exclude: Comma-separated exclude globs.
        :param limit: Maximum results (capped at 500 by the caller).
        :returns: A list payload of matching file and directory entries. In a
            git workspace tracked files come from the index and untracked
            ones from the latest ``git status`` when one has run; a budgeted
            walk always runs too so ignored files stay findable, and
            ``truncated`` says if it ran out.
        """
        q = query.strip().lower()
        if not q:
            return {"object": "list", "data": [], "has_more": False}

        include_patterns = split_glob_list(include)
        exclude_patterns = split_glob_list(exclude)
        inc = [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in include_patterns]
        exc = [re.compile(_glob_to_regex(p), re.IGNORECASE) for p in exclude_patterns]

        # The budgeted walk below is the only source for ignored files (and,
        # until a ``git status`` has run, untracked ones), so it always runs;
        # git's index adds every tracked file however large the repo, and the
        # latest ``git status`` adds untracked files past the budget.
        indexed = index_search(
            self._registry,
            self._root,
            "",
            q,
            include=include_patterns,
            exclude=exclude_patterns,
            limit=limit,
        )

        results, truncated = walk_search(
            self._root, q, inc, exc, limit, budget=_SEARCH_SCAN_BUDGET
        )
        if indexed is not None:
            results = merge_entries(indexed, results, limit)
        return self._search_payload(results, limit, truncated=truncated)

    @staticmethod
    def _search_payload(
        entries: list[FilesystemEntry], limit: int, *, truncated: bool
    ) -> _WorkspacePayload:
        """Wrap search *entries* in the runner's ``/search`` response shape.

        :param entries: Matching entries, already sorted and capped.
        :param limit: The cap, so ``has_more`` reads the same as the runner's.
        :param truncated: Whether the scan stopped before covering the tree.
        :returns: The list payload.
        """
        return {
            "object": "list",
            "data": [entry_payload(e) for e in entries],
            "has_more": len(entries) >= limit,
            "truncated": truncated,
        }

    # ── Changed files / diff ───────────────────────────────────────

    def changes(self, session_id: str) -> _WorkspacePayload:
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
                "untracked_lines": rec.get("untracked_lines"),
            }
            for rec in raw_changes
        ]
        return {"object": "list", "data": data, "has_more": False}

    def diff(self, session_id: str, relative_path: str) -> _WorkspacePayload:
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

    # ── Pull request panel (read-only) ────────────────────────────
    # Serve the same read-only PR metadata + the PR's files / diff the runner's
    # pull request endpoints do, so the tab keeps working when the runner is
    # offline but the host still holds the workspace. Delegates to the shared
    # ``pr_resource`` dispatcher against this reader's confined root, which runs
    # the git provider's CLI or API (``gh`` for GitHub).

    def github_info(
        self, session_id: str | None = None, pr_url: str | None = None
    ) -> _WorkspacePayload:
        """PR context for an explicit session PR or the workspace branch."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_info(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
            ),
        )

    def github_review_threads(
        self,
        session_id: str,
        pr_url: str,
        after: str,
        head_sha: str,
        base_sha: str,
        *,
        account_rights: bool = False,
    ) -> _WorkspacePayload:
        """Published review conversations, optionally paginated at a requested revision.

        :param account_rights: Whether the server asked for the host account's rights.
        """
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_review_threads(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                after=after,
                head_sha=head_sha,
                base_sha=base_sha,
                account_rights=account_rights,
            ),
        )

    def github_review_thread_comments(
        self,
        session_id: str,
        pr_url: str,
        thread_id: str,
        after: str,
        *,
        account_rights: bool = False,
    ) -> _WorkspacePayload:
        """Additional replies in a selected-PR review conversation; see above."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_review_thread_comments(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                thread_id=thread_id,
                after=after,
                account_rights=account_rights,
            ),
        )

    def github_changes(
        self,
        session_id: str | None = None,
        pr_url: str | None = None,
        *,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> _WorkspacePayload:
        """The selected PR's changed files."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_changed_files(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                head_sha=head_sha,
                base_sha=base_sha,
            ),
        )

    def github_file_diff(
        self,
        base: str | None,
        relative_path: str,
        session_id: str | None = None,
        pr_url: str | None = None,
        previous_path: str | None = None,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> _WorkspacePayload:
        """Before/after content for the selected PR's revisions."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_file_diff(
                str(self._root),
                base or "",
                relative_path,
                session_id=session_id,
                pr_url=pr_url,
                previous_path=previous_path,
                head_sha=head_sha,
                base_sha=base_sha,
            ),
        )

    def github_pr_diff(
        self,
        session_id: str | None = None,
        pr_url: str | None = None,
        *,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> _WorkspacePayload:
        """The selected PR's unified diff patch."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_diff(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                head_sha=head_sha,
                base_sha=base_sha,
            ),
        )

    def github_commits(
        self,
        session_id: str | None = None,
        pr_url: str | None = None,
        *,
        head_sha: str | None = None,
    ) -> _WorkspacePayload:
        """The selected PR's commits at the displayed head."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_commits(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                head_sha=head_sha,
            ),
        )

    def github_commit_diff(
        self,
        session_id: str | None = None,
        pr_url: str | None = None,
        *,
        commit_sha: str | None = None,
    ) -> _WorkspacePayload:
        """One commit's unified diff patch from the selected PR."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_commit_diff(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                commit_sha=commit_sha,
            ),
        )

    def github_compare_diff(
        self,
        session_id: str | None = None,
        pr_url: str | None = None,
        *,
        head_sha: str | None = None,
        from_sha: str | None = None,
    ) -> _WorkspacePayload:
        """The selected PR's changes after one of its commits."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_compare_diff(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                head_sha=head_sha,
                from_sha=from_sha,
            ),
        )

    def github_labels(
        self, session_id: str | None = None, pr_url: str | None = None, text: str = ""
    ) -> _WorkspacePayload:
        """The labels the selected PR's repository offers, or those matching ``text``."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_labels(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
                text=text,
            ),
        )

    def github_merge_options(
        self, session_id: str | None = None, pr_url: str | None = None
    ) -> _WorkspacePayload:
        """The merge methods the selected PR's repository allows."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_merge_options(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
            ),
        )

    def github_reviewers(
        self, session_id: str | None = None, pr_url: str | None = None
    ) -> _WorkspacePayload:
        """The users and teams a review of the selected PR can be requested from."""
        return cast(
            "_WorkspacePayload",
            pr_resource.pr_review_candidates(
                str(self._root),
                session_id=session_id,
                pr_url=pr_url,
            ),
        )
