"""File entity."""

from dataclasses import dataclass


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
    :param blob_key: Artifact-store key holding this file's bytes.
        Normally equals ``id`` (each upload owns its blob), but a
        forked file row points at the source's blob so the fork copies
        no bytes — many rows can then share one blob. ``None`` on
        pre-``blob_key`` rows means "the blob is under ``id``"; read
        the bytes with ``blob_key or id``.
    """

    id: str
    created_at: int
    filename: str
    bytes: int
    content_type: str | None = None
    session_id: str | None = None
    blob_key: str | None = None
