"""Typed domain objects for environment filesystem operations.

These data classes carry filesystem operation inputs, outputs, and
errors across the runner's internal API and the public REST
surface.  They replace the opaque ``dict[str, Any]`` (``OpResult``)
shape used by the legacy ``OSEnvironment.read/write/edit`` methods.

See ``designs/SESSION_RESOURCES_API_DESIGN.md`` §Environment
filesystem service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ── Filesystem entries ───────────────────────────────────────────


@dataclass(frozen=True)
class FilesystemEntry:
    """Metadata for a single file or directory in an environment.

    :param id: Stable identifier within a directory listing,
        typically the entry's relative path.
    :param name: Base name of the entry, e.g. ``"main.py"``.
    :param path: Path relative to the environment root.
    :param type: Entry type discriminator.
    :param bytes: Size in bytes for files; ``None`` for directories.
    :param modified_at: Unix epoch seconds of last modification.
    """

    id: str
    name: str
    path: str
    type: Literal["file", "directory", "symlink", "other"]
    bytes: int | None = None
    modified_at: int | None = None


@dataclass(frozen=True)
class FileContent:
    """Content read from an environment file.

    :param path: Relative path within the environment.
    :param data: Raw file bytes.
    :param bytes: Size of ``data``.
    :param encoding: Detected or requested encoding, e.g.
        ``"utf-8"``. ``None`` for binary content.
    :param truncated: Whether the content was truncated due to
        a size limit.
    """

    path: str
    data: bytes
    bytes: int
    encoding: str | None = None
    truncated: bool = False


# ── Operation results ────────────────────────────────────────────


@dataclass(frozen=True)
class WriteFileResult:
    """Result of a file write operation.

    :param operation: Always ``"write"``.
    :param path: Relative path within the environment.
    :param created: Whether the file was newly created.
    :param bytes_written: Number of bytes written.
    :param entry: Resulting filesystem entry metadata.
    """

    path: str
    operation: Literal["write"] = "write"
    created: bool = False
    bytes_written: int = 0
    entry: FilesystemEntry | None = None


@dataclass(frozen=True)
class PageRequest:
    """Pagination parameters for list operations.

    :param limit: Maximum items to return, default 20.
    :param after: Cursor id for forward pagination.
    :param before: Cursor id for backward pagination.
    :param order: Sort direction, ``"asc"`` or ``"desc"``.
    """

    limit: int = 20
    after: str | None = None
    before: str | None = None
    order: Literal["asc", "desc"] = "desc"


@dataclass(frozen=True)
class TextReplacement:
    """A single old/new text pair for batch edits.

    :param old_text: Text to find.
    :param new_text: Replacement text.
    """

    old_text: str
    new_text: str


@dataclass(frozen=True)
class TextEditRequest:
    """Request body for a text edit operation.

    Supports either a single ``old_text``/``new_text`` pair or a
    batch ``edits`` list for multiple replacements.  Callers must
    provide one or the other, not both.

    :param old_text: Text to find and replace (single mode).
    :param new_text: Replacement text (single mode).
    :param edits: Batch list of replacements (batch mode).
    :param replace_all: Replace all occurrences of each
        old_text (not just first).
    """

    old_text: str | None = None
    new_text: str | None = None
    edits: list[TextReplacement] | None = None
    replace_all: bool = False


@dataclass(frozen=True)
class EditFileResult:
    """Result of a text edit operation.

    :param operation: Always ``"edit"``.
    :param path: Relative path within the environment.
    :param replacements: Number of replacements applied.
    :param bytes_before: File size before edit.
    :param bytes_after: File size after edit.
    :param entry: Resulting filesystem entry metadata.
    """

    path: str
    operation: Literal["edit"] = "edit"
    replacements: int = 0
    bytes_before: int | None = None
    bytes_after: int | None = None
    entry: FilesystemEntry | None = None


@dataclass(frozen=True)
class DeleteFilesystemResult:
    """Result of a filesystem delete operation.

    :param operation: Always ``"delete"``.
    :param path: Relative path within the environment.
    :param deleted: Whether the path was successfully deleted.
    :param type: Type of the deleted entry.
    :param bytes_deleted: Bytes freed, if known.
    :param entries_deleted: Count of entries deleted for recursive
        directory deletes.
    """

    path: str
    operation: Literal["delete"] = "delete"
    deleted: bool = False
    type: Literal["file", "directory", "symlink", "other"] = "file"
    bytes_deleted: int | None = None
    entries_deleted: int | None = None


# ── Typed exceptions ─────────────────────────────────────────────


class ResourceError(Exception):
    """Base exception for resource operations.

    :param message: Human-readable error description.
    :param code: Stable error code string for API translation.
    """

    code: str = "resource_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidPath(ResourceError):
    """The path is malformed or escapes the environment root.

    :param message: Description of the path violation.
    """

    code = "invalid_path"


class ResourceNotFound(ResourceError):
    """A resource (session, environment, terminal) was not found.

    :param message: Description including the missing resource.
    """

    code = "resource_not_found"


class FilesystemPathNotFound(ResourceError):
    """The requested path does not exist.

    :param message: Description including the missing path.
    """

    code = "path_not_found"


class DirectoryNotEmpty(ResourceError):
    """A non-empty directory cannot be deleted without recursive=True.

    :param message: Description of the non-empty directory.
    """

    code = "directory_not_empty"


class FileTooLarge(ResourceError):
    """The file exceeds the configured size limit.

    :param message: Description including the size and limit.
    """

    code = "file_too_large"


class UnsupportedMediaType(ResourceError):
    """The file is binary and cannot be served as text.

    :param message: Description of the unsupported content.
    """

    code = "unsupported_media_type"


# ── Shell / process types ─────────────────────────────────────────


@dataclass(frozen=True)
class ShellResult:
    """Result of a shell command execution.

    :param stdout: Standard output of the command.
    :param stderr: Standard error of the command.
    :param exit_code: Process exit code, or ``None`` when no status exists.
    :param timed_out: Whether the command was killed by timeout.
    :param cwd: Working directory the command ran in, if known.
    """

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    cwd: str | None = None


class PermissionDenied(ResourceError):
    """The operation is denied by the sandbox or access policy.

    :param message: Description of the denied operation.
    """

    code = "permission_denied"
