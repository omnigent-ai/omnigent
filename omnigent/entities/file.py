"""File entity."""

from dataclasses import dataclass

FILE_PURPOSE_USER_UPLOAD = "user_upload"
FILE_PURPOSE_COMPUTER_USE_FRAME = "computer_use_frame"
FILE_PURPOSES = frozenset(
    {
        FILE_PURPOSE_USER_UPLOAD,
        FILE_PURPOSE_COMPUTER_USE_FRAME,
    }
)


@dataclass
class StoredFile:
    """
    A stored file with metadata.

    :param id: Unique file identifier, e.g. ``"file_abc123"``.
    :param created_at: Unix epoch timestamp of upload.
    :param filename: Original filename, e.g. ``"report.pdf"``.
    :param bytes: File size in bytes.
    :param content_type: MIME type, e.g. ``"application/pdf"``.
    :param session_id: Owning session/conversation id when the file
        is session-scoped, e.g. ``"conv_abc123"``. ``None`` for
        historical unscoped records created before session-scoped
        file resources were introduced.
    :param purpose: Artifact visibility/use class. ``"user_upload"`` files
        appear in normal file lists. ``"computer_use_frame"`` files are
        generated previews hidden from those lists.
    """

    id: str
    created_at: int
    filename: str
    bytes: int
    content_type: str | None = None
    session_id: str | None = None
    purpose: str = FILE_PURPOSE_USER_UPLOAD
