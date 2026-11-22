"""File store — manages file metadata."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import PagedList, StoredFile


class FileStore(ABC):
    """
    Abstract base for file metadata persistence.

    Tracks file metadata (filename, size, content type). Binary
    content is managed separately by :class:`ArtifactStore`.

    A file belongs to a session (``session_id``) or to a project
    (``project_id``), never both. ``create`` stamps the owner, ``get``
    and ``delete`` verify it when given one, and ``list`` /
    ``list_for_project`` return one owner's files. A row with neither is
    a historical unscoped ("global") file; new callers always pass an
    owner, and ``None`` otherwise exists only for low-level tests.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the file store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///files.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        filename: str,
        bytes: int,
        content_type: str | None = None,
        session_id: str | None = None,
        file_id: str | None = None,
        blob_key: str | None = None,
        source_metadata: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> StoredFile:
        """
        Record a new file. Generates a unique file_id unless given one.

        :param filename: Original filename,
            e.g. ``"report.pdf"``.
        :param bytes: File size in bytes.
        :param content_type: MIME type of the file,
            e.g. ``"application/pdf"``.
        :param session_id: Owning session/conversation id. When
            set, the file is session-scoped; ``None`` for global.
        :param file_id: Caller-chosen id for the new row. A fork
            pre-allocates ids so the copied conversation items can
            reference the copies it creates afterwards. ``None``
            (default) generates a fresh id.
        :param blob_key: Artifact-store key for the row's bytes. A fork
            copy passes the source row's blob so it shares the bytes
            instead of duplicating them; ``None`` (default) points the
            row at its own ``file_id`` (an independent blob).
        :param source_metadata: Optional opaque JSON-able dict of
            metadata about the original upload before any server-side
            transform (e.g. ``{"width", "height"}`` for a downscaled
            image), or ``None`` when there is nothing to record.
        :param project_id: Owning project for a project file, e.g.
            ``"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"``. Mutually exclusive
            with ``session_id``.
        :returns: The newly created :class:`StoredFile`.
        :raises ValueError: If both ``session_id`` and ``project_id`` are set.
        """
        ...

    @abstractmethod
    def is_blob_key_orphaned(self, blob_key: str) -> bool:
        """
        Whether no file row references *blob_key* any more.

        A blob is shared when a fork copies a file row without copying its
        bytes, so the artifact-store blob must only be deleted once the last
        referencing row is gone. Callers delete the row first, then consult
        this before removing the blob.

        :param blob_key: The artifact-store key to test.
        :returns: ``True`` when the blob has no remaining referrers and is
            safe to delete.
        """
        ...

    @abstractmethod
    def get(
        self,
        file_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> StoredFile | None:
        """
        Return the file metadata, or ``None`` if not found.

        When ``session_id`` or ``project_id`` is set, only returns the
        file if it belongs to that session or project.

        :param file_id: Unique file identifier,
            e.g. ``"file_abc123"``.
        :param session_id: If set, verify the file belongs to
            this session.
        :param project_id: If set, verify the file belongs to this
            project. With neither set, any file is returned.
        :returns: The :class:`StoredFile` if found (and owned
            when an owner is given), otherwise ``None``.
        """
        ...

    @abstractmethod
    def list(
        self,
        session_id: str,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        include_unscoped: bool = False,
    ) -> PagedList[StoredFile]:
        """
        List a session's files with cursor-based pagination.

        Always scoped to a session — there is no cross-session
        listing (files are only ever surfaced per session).

        :param session_id: Owning session whose files to list.
        :param limit: Maximum number of files to return.
        :param after: Cursor file ID for forward pagination.
        :param before: Cursor file ID for backward pagination.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param include_unscoped: When ``True``, also return global
            files, which belong to no session and no project.
        :returns: A :class:`PagedList` of :class:`StoredFile`.
        """
        ...

    @abstractmethod
    def list_for_project(
        self,
        project_id: str,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> PagedList[StoredFile]:
        """
        List a project's files with cursor-based pagination.

        :param project_id: Owning project whose files to list, e.g.
            ``"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"``.
        :param limit: Maximum number of files to return.
        :param after: Cursor file ID for forward pagination.
        :param before: Cursor file ID for backward pagination.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :returns: A :class:`PagedList` of :class:`StoredFile`.
        """
        ...

    @abstractmethod
    def delete(
        self,
        file_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        """
        Delete file metadata.

        When ``session_id`` or ``project_id`` is set, only deletes if
        the file belongs to that session or project. Returns ``False``
        if not found or not owned.

        :param file_id: Unique file identifier.
        :param session_id: If set, verify session ownership before
            deleting.
        :param project_id: If set, verify project ownership before
            deleting. With neither set, any file is deleted.
        :returns: ``True`` if deleted, ``False`` otherwise.
        """
        ...

    @abstractmethod
    def delete_all_for_session(self, session_id: str) -> builtins.list[str]:
        """
        Delete all file metadata for a session.

        Returns only the artifact-store keys that became **orphaned** by the
        deletion (no surviving row references them), so the caller cleans up
        exactly those bytes. A blob still shared by a fork in another session
        is not returned and therefore survives.

        :param session_id: Owning session/conversation id.
        :returns: The now-orphaned artifact-store keys to clean up.
        """
        ...

    @abstractmethod
    def delete_all_for_project(self, project_id: str) -> builtins.list[str]:
        """
        Delete all file metadata for a project.

        Like :meth:`delete_all_for_session`, returns only the artifact-store
        keys that no surviving row references, so the caller deletes exactly
        those bytes.

        :param project_id: Owning project id, e.g.
            ``"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"``.
        :returns: The now-orphaned artifact-store keys to clean up.
        """
        ...
