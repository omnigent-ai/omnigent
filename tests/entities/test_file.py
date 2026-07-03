"""Tests for file entity dataclass."""

from __future__ import annotations

from omnigent.entities.file import StoredFile


def test_stored_file_minimal() -> None:
    f = StoredFile(
        id="file_abc123",
        created_at=1700000000,
        filename="report.pdf",
        bytes=1024,
    )
    assert f.id == "file_abc123"
    assert f.filename == "report.pdf"
    assert f.bytes == 1024
    assert f.content_type is None
    assert f.session_id is None


def test_stored_file_full() -> None:
    f = StoredFile(
        id="file_xyz",
        created_at=1700000000,
        filename="image.png",
        bytes=204800,
        content_type="image/png",
        session_id="conv_abc123",
    )
    assert f.content_type == "image/png"
    assert f.session_id == "conv_abc123"


def test_stored_file_is_mutable() -> None:
    f = StoredFile(id="f1", created_at=1, filename="a.txt", bytes=10)
    f.filename = "b.txt"
    assert f.filename == "b.txt"
