"""File store — manages file metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import PagedList, StoredFile


class FileStore(ABC):
    """
    Abstract base for file metadata persistence.

    Tracks file metadata (filename, size, content type). Binary
    content is managed separately by :class:`ArtifactStore`.

    All methods accept an optional ``session_id`` to scope
    operations to a specific session. New callers should pass a
    session id. ``None`` exists only for historical unscoped rows
    and low-level tests. When set, ``create`` stamps the file with
    session ownership, ``get`` and ``delete`` verify ownership, and
    ``list`` filters to that session's files only.
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
    ) -> StoredFile:
        """
        Record a new file. Generates a unique file_id.

        :param filename: Original filename,
            e.g. ``"report.pdf"``.
        :param bytes: File size in bytes.
        :param content_type: MIME type of the file,
            e.g. ``"application/pdf"``.
        :param session_id: Owning session/conversation id. When
            set, the file is session-scoped; ``None`` for global.
        :returns: The newly created :class:`StoredFile`.
        """
        ...

    @abstractmethod
    def get(
        self,
        file_id: str,
        session_id: str | None = None,
    ) -> StoredFile | None:
        """
        Return the file metadata, or ``None`` if not found.

        When ``session_id`` is set, only returns the file if it
        belongs to that session.

        :param file_id: Unique file identifier,
            e.g. ``"file_abc123"``.
        :param session_id: If set, verify the file belongs to
            this session. ``None`` returns any file.
        :returns: The :class:`StoredFile` if found (and owned
            when session_id is set), otherwise ``None``.
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
        :param include_unscoped: When ``True``, also return files
            with ``session_id IS NULL`` (global/unscoped files).
        :returns: A :class:`PagedList` of :class:`StoredFile`.
        """
        ...

    @abstractmethod
    def delete(
        self,
        file_id: str,
        session_id: str | None = None,
    ) -> bool:
        """
        Delete file metadata.

        When ``session_id`` is set, only deletes if the file
        belongs to that session. Returns ``False`` if not found
        or not owned.

        :param file_id: Unique file identifier.
        :param session_id: If set, verify ownership before
            deleting. ``None`` deletes any file.
        :returns: ``True`` if deleted, ``False`` otherwise.
        """
        ...

    @abstractmethod
    def delete_all_for_session(self, session_id: str) -> list[str]:
        """
        Delete all file metadata for a session.

        Returns the list of deleted file ids so callers can
        clean up artifact bytes.

        :param session_id: Owning session/conversation id.
        :returns: List of deleted file ids.
        """
        ...
