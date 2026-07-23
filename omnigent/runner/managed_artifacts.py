"""Runner-owned storage for session design artifacts."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.entities.environment_filesystem import (
    FileContent,
    FilesystemPathNotFound,
    InvalidPath,
)
from omnigent.runner.identity import ARTIFACT_DIR_ENV_VAR

_MAX_READ_BYTES = 10 * 1024 * 1024
PUBLISH_DESIGN_ARTIFACT_TOOL = "publish_design_artifact"
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ManagedArtifactEntry:
    """One canonical HTML artifact entry exposed through the virtual namespace."""

    path: str
    name: str
    bytes: int
    modified_at: float


def _data_dir() -> Path:
    configured = os.environ.get("OMNIGENT_DATA_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".omnigent"


def managed_artifact_dir(session_id: str) -> Path:
    """Return the host-local managed artifact directory for one session."""
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        raise ValueError("session_id contains unsupported path characters")
    return _data_dir() / "artifacts" / "sessions" / session_id


def artifact_spawn_env(session_id: str) -> dict[str, str]:
    """Return the session-scoped environment contract exposed to agents."""
    root = managed_artifact_dir(session_id)
    root.mkdir(parents=True, exist_ok=True)
    return {ARTIFACT_DIR_ENV_VAR: str(root)}


def with_artifact_spawn_env(
    spawn_env: dict[str, str] | None,
    session_id: str,
) -> dict[str, str]:
    """Merge the managed artifact directory into a harness spawn environment."""
    return {**(spawn_env or {}), **artifact_spawn_env(session_id)}


def _virtual_parts(path: str) -> list[str]:
    if not path or "\\" in path or path.startswith("/"):
        raise InvalidPath("artifact path must be a relative POSIX path")
    parts = path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise InvalidPath("artifact path must be normalized")
    if parts[0] != "artifacts" or len(parts) < 2:
        raise InvalidPath("artifact path must be under artifacts/")
    return parts


def _artifact_relative_parts(path: str) -> list[str]:
    return _virtual_parts(path)[1:]


def is_managed_artifact_namespace(path: object) -> bool:
    """Return whether a tool path targets the virtual artifact namespace."""
    return isinstance(path, str) and (path == "artifacts" or path.startswith("artifacts/"))


def discover_managed_artifacts(session_id: str) -> list[ManagedArtifactEntry]:
    """List canonical standalone and one-directory HTML entry points."""
    root = managed_artifact_dir(session_id)
    if not root.is_dir():
        return []
    entries: list[ManagedArtifactEntry] = []
    with os.scandir(root) as children:
        for child in children:
            if child.is_file(follow_symlinks=False) and child.name.lower().endswith(".html"):
                info = child.stat(follow_symlinks=False)
                entries.append(
                    ManagedArtifactEntry(
                        path=f"artifacts/{child.name}",
                        name=child.name,
                        bytes=info.st_size,
                        modified_at=info.st_mtime,
                    )
                )
                continue
            if not child.is_dir(follow_symlinks=False):
                continue
            index_path = Path(child.path) / "index.html"
            try:
                info = index_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                entries.append(
                    ManagedArtifactEntry(
                        path=f"artifacts/{child.name}/index.html",
                        name="index.html",
                        bytes=info.st_size,
                        modified_at=info.st_mtime,
                    )
                )
    return sorted(entries, key=lambda entry: entry.path)


def _open_managed_parent(
    session_id: str,
    path: str,
    *,
    create_directories: bool,
) -> tuple[int, str]:
    parts = _artifact_relative_parts(path)
    root = managed_artifact_dir(session_id)
    if create_directories:
        root.mkdir(parents=True, exist_ok=True)
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(root, flags_dir)
    try:
        for part in parts[:-1]:
            if create_directories:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=directory_fd)
            next_fd = os.open(part, flags_dir, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd, parts[-1]


def _read_managed_bytes_sync(session_id: str, path: str, *, max_bytes: int) -> bytes:
    _virtual_parts(path)
    if max_bytes < 1 or max_bytes > _MAX_READ_BYTES:
        raise InvalidPath("max_bytes is outside the allowed range")

    flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd, filename = _open_managed_parent(
            session_id,
            path,
            create_directories=False,
        )
        try:
            file_fd = os.open(filename, flags_file, dir_fd=directory_fd)
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                    raise OSError("invalid artifact resource")
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    chunk = os.read(file_fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                if len(data) > max_bytes:
                    raise OSError("artifact resource too large")
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise FilesystemPathNotFound(f"Artifact resource {path!r} not found") from exc

    return data


def _read_managed_artifact_sync(
    session_id: str,
    path: str,
    *,
    artifact_root: str,
    max_bytes: int,
) -> FileContent:
    path_parts = _virtual_parts(path)
    root_parts = _virtual_parts(artifact_root)
    if path_parts == root_parts:
        allowed = artifact_root.lower().endswith(".html")
    else:
        allowed = path_parts[
            : len(root_parts)
        ] == root_parts and not artifact_root.lower().endswith(".html")
    if not allowed:
        raise InvalidPath(f"Path {path!r} is outside artifact root {artifact_root!r}")
    data = _read_managed_bytes_sync(session_id, path, max_bytes=max_bytes)

    return FileContent(
        path=path,
        data=data,
        bytes=len(data),
        encoding=None,
        truncated=False,
    )


def _write_managed_text_sync(session_id: str, path: str, content: str) -> dict[str, Any]:
    payload = content.encode("utf-8")
    if len(payload) > _MAX_READ_BYTES:
        raise InvalidPath("artifact file is too large")
    directory_fd, filename = _open_managed_parent(
        session_id,
        path,
        create_directories=True,
    )
    temporary_name = f".omnigent-write-{secrets.token_hex(16)}"
    temporary_fd: int | None = None
    existed = False
    try:
        try:
            current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode):
                raise InvalidPath("artifact destination must be a regular file")
            existed = True
        except FileNotFoundError:
            pass
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(temporary_fd, view)
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except OSError as exc:
        raise InvalidPath(f"Could not write managed artifact {path!r}") from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)
    return {
        "path": path,
        "bytes_written": len(payload),
        "created": not existed,
    }


def _read_managed_text_sync(
    session_id: str,
    path: str,
    *,
    offset: int,
    limit: int | None,
) -> dict[str, Any]:
    if offset < 1:
        return {"error": "offset must be >= 1"}
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        return {"error": "limit must be >= 1"}
    try:
        text = _read_managed_bytes_sync(session_id, path, max_bytes=_MAX_READ_BYTES).decode(
            "utf-8"
        )
    except UnicodeDecodeError:
        return {"error": "managed artifact file is not UTF-8 text"}
    lines = text.splitlines(keepends=True)
    start = offset - 1
    effective_limit = len(lines) if limit is None else limit
    end = min(len(lines), start + effective_limit)
    return {
        "path": path,
        "content": "".join(lines[start:end]),
        "encoding": "utf-8",
        "offset": offset,
        "limit": effective_limit,
        "returned_lines": max(0, end - start),
        "total_lines": len(lines),
    }


def _normalize_edits(
    old_text: object,
    new_text: object,
    edits: object,
) -> list[tuple[str, str]]:
    if edits is not None and (old_text is not None or new_text is not None):
        raise ValueError("Provide either oldText/newText or edits, not both")
    if edits is not None:
        if not isinstance(edits, list):
            raise ValueError("edits must be an array of {oldText, newText} objects")
        normalized: list[tuple[str, str]] = []
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError("Each edit must be an object")
            before = edit.get("oldText")
            after = edit.get("newText")
            if not isinstance(before, str) or not isinstance(after, str):
                raise ValueError("Each edit must contain string oldText and newText")
            normalized.append((before, after))
        return normalized
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise ValueError("edit requires oldText/newText or edits")
    return [(old_text, new_text)]


def _edit_managed_text_sync(
    session_id: str,
    path: str,
    *,
    old_text: object,
    new_text: object,
    edits: object,
) -> dict[str, Any]:
    original = _read_managed_bytes_sync(session_id, path, max_bytes=_MAX_READ_BYTES).decode(
        "utf-8"
    )
    try:
        replacements = _normalize_edits(old_text, new_text, edits)
    except ValueError as exc:
        return {"error": str(exc)}
    updated = original
    for before, after in replacements:
        count = updated.count(before)
        if count == 0:
            return {"error": f"Could not find oldText in '{path}': {before[:80]!r}"}
        if count > 1:
            return {
                "error": (
                    f"oldText matched {count} locations in '{path}'; provide a more specific edit."
                )
            }
        updated = updated.replace(before, after, 1)
    result = _write_managed_text_sync(session_id, path, updated)
    return {
        "path": path,
        "replacements": len(replacements),
        "bytes_written": result["bytes_written"],
    }


async def read_managed_artifact(
    session_id: str,
    path: str,
    *,
    artifact_root: str,
    max_bytes: int = _MAX_READ_BYTES,
) -> FileContent:
    """Atomically read a managed artifact resource without following symlinks."""
    return await asyncio.to_thread(
        _read_managed_artifact_sync,
        session_id,
        path,
        artifact_root=artifact_root,
        max_bytes=max_bytes,
    )


def _publish_managed_artifact_sync(
    session_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    entry_path = arguments.get("entry_path")
    title = arguments.get("title")
    operation = arguments.get("operation", "created")
    summary = arguments.get("summary")
    if not isinstance(entry_path, str):
        raise InvalidPath("entry_path must be a string")
    _virtual_parts(entry_path)
    if not entry_path.lower().endswith(".html"):
        raise InvalidPath("entry_path must point to an HTML file")
    if not isinstance(title, str) or not title.strip():
        raise InvalidPath("title must not be empty")
    if operation not in {"created", "updated"}:
        raise InvalidPath("operation must be created or updated")
    if summary is not None and not isinstance(summary, str):
        raise InvalidPath("summary must be a string")

    _read_managed_bytes_sync(session_id, entry_path, max_bytes=_MAX_READ_BYTES)
    parts = _artifact_relative_parts(entry_path)
    physical_entry = managed_artifact_dir(session_id).joinpath(*parts)
    virtual_root = (
        entry_path.rsplit("/", 1)[0] if physical_entry.name == "index.html" else entry_path
    )
    physical_root = (
        physical_entry.parent if physical_entry.name == "index.html" else physical_entry
    )
    resource_count = 1
    if physical_root.is_dir():
        resource_count = 0
        for directory, directory_names, filenames in os.walk(physical_root, followlinks=False):
            directory_names[:] = [
                name for name in directory_names if not (Path(directory) / name).is_symlink()
            ]
            for filename in filenames:
                candidate = Path(directory) / filename
                try:
                    info = candidate.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    resource_count += 1

    return {
        "ok": True,
        "entry_path": entry_path,
        "artifact_root": virtual_root,
        "title": title.strip(),
        "operation": operation,
        "language": "html",
        "resource_count": resource_count,
        "summary": summary.strip() if isinstance(summary, str) and summary.strip() else None,
    }


async def publish_managed_artifact(
    session_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Validate and publish a session-managed artifact entry point."""
    return await asyncio.to_thread(_publish_managed_artifact_sync, session_id, arguments)


async def write_managed_artifact_text(
    session_id: str,
    path: str,
    content: str,
) -> dict[str, Any]:
    """Atomically write UTF-8 text through the virtual artifact namespace."""
    return await asyncio.to_thread(_write_managed_text_sync, session_id, path, content)


async def read_managed_artifact_text(
    session_id: str,
    path: str,
    *,
    offset: int = 1,
    limit: int | None = None,
) -> dict[str, Any]:
    """Read UTF-8 text through the virtual artifact namespace."""
    return await asyncio.to_thread(
        _read_managed_text_sync,
        session_id,
        path,
        offset=offset,
        limit=limit,
    )


async def edit_managed_artifact_text(
    session_id: str,
    path: str,
    *,
    old_text: object = None,
    new_text: object = None,
    edits: object = None,
) -> dict[str, Any]:
    """Apply exact text replacements through the virtual artifact namespace."""
    return await asyncio.to_thread(
        _edit_managed_text_sync,
        session_id,
        path,
        old_text=old_text,
        new_text=new_text,
        edits=edits,
    )
