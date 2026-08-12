"""Tests for bounded Computer Use preview-frame storage."""

from __future__ import annotations

import pytest

from omnigent.computer_use_frames import (
    ComputerUseFrameLimits,
    image_dimensions,
    store_computer_use_frame,
)
from omnigent.entities import FILE_PURPOSE_COMPUTER_USE_FRAME
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

_SESSION = "79b22ebd2309e48fdeb450c65611d51b"
_OTHER_SESSION = "5d29bee4350489d66feafecfebd94a97"


def _png(width: int = 4, height: int = 3, suffix: bytes = b"") -> bytes:
    """Build the bounded PNG header used by the header parser."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + suffix
    )


def _jpeg(width: int = 4, height: int = 3) -> bytes:
    """Build a minimal JPEG SOF0 segment for dimension parsing."""
    return (
        b"\xff\xd8\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )


def _webp(width: int = 4, height: int = 3) -> bytes:
    """Build a minimal VP8X header for dimension parsing."""
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little")
    payload += (height - 1).to_bytes(3, "little")
    riff_size = (4 + 8 + len(payload)).to_bytes(4, "little")
    return b"RIFF" + riff_size + b"WEBPVP8X" + len(payload).to_bytes(4, "little") + payload


class MemoryArtifactStore(ArtifactStore):
    """Small artifact store that can simulate a partial write failure."""

    def __init__(self) -> None:
        super().__init__("memory://computer-use-tests")
        self.blobs: dict[str, bytes] = {}
        self.fail_put = False
        self.fail_delete_ids: set[str] = set()

    def put(self, key: str, data: bytes) -> None:
        self.blobs[key] = data
        if self.fail_put:
            raise RuntimeError("simulated write failure")

    def get(self, key: str) -> bytes:
        return self.blobs[key]

    def delete(self, key: str) -> None:
        if key in self.fail_delete_ids:
            raise RuntimeError("simulated delete failure")
        self.blobs.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.blobs


@pytest.fixture
def file_store(db_uri: str) -> SqlAlchemyFileStore:
    return SqlAlchemyFileStore(db_uri)


@pytest.fixture
def artifact_store() -> MemoryArtifactStore:
    return MemoryArtifactStore()


def test_image_dimensions_accepts_supported_headers() -> None:
    assert image_dimensions(_png(7, 5), "image/png") == (7, 5)
    assert image_dimensions(_jpeg(7, 5), "image/jpeg") == (7, 5)
    assert image_dimensions(_webp(7, 5), "image/webp") == (7, 5)


def test_store_frame_is_hidden_and_session_owned(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
) -> None:
    attachment = store_computer_use_frame(
        file_store=file_store,
        artifact_store=artifact_store,
        session_id=_SESSION,
        data=_png(7, 5),
        content_type="image/png",
    )

    assert attachment.model_dump() == {
        "kind": "computer_frame",
        "file_id": attachment.file_id,
        "content_type": "image/png",
        "width": 7,
        "height": 5,
    }
    assert attachment.file_id is not None
    stored = file_store.get(attachment.file_id, session_id=_SESSION)
    assert stored is not None
    assert stored.purpose == FILE_PURPOSE_COMPUTER_USE_FRAME
    assert artifact_store.get(stored.id) == _png(7, 5)
    assert file_store.get(stored.id, session_id=_OTHER_SESSION) is None
    assert file_store.list(session_id=_SESSION).data == []
    assert [
        frame.id
        for frame in file_store.list(
            session_id=_SESSION,
            purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
        ).data
    ] == [stored.id]


def test_store_frame_dedup_key_reuses_hidden_artifact(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
) -> None:
    first = store_computer_use_frame(
        file_store=file_store,
        artifact_store=artifact_store,
        session_id=_SESSION,
        data=_png(7, 5),
        content_type="image/png",
        dedup_key="codex:turn_1:call_1:0",
    )
    second = store_computer_use_frame(
        file_store=file_store,
        artifact_store=artifact_store,
        session_id=_SESSION,
        data=_png(7, 5),
        content_type="image/png",
        dedup_key="codex:turn_1:call_1:0",
    )

    assert second == first
    frames = file_store.list(
        session_id=_SESSION,
        purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
    ).data
    assert [frame.id for frame in frames] == [first.file_id]


@pytest.mark.parametrize(
    ("data", "content_type", "limits", "message"),
    [
        (b"", "image/png", ComputerUseFrameLimits(), "empty"),
        (b"not an image", "image/png", ComputerUseFrameLimits(), "PNG header"),
        (_png(), "image/gif", ComputerUseFrameLimits(), "unsupported"),
        (
            _png(20, 1),
            "image/png",
            ComputerUseFrameLimits(max_dimension=10),
            "dimension limit",
        ),
        (
            _png(5, 5),
            "image/png",
            ComputerUseFrameLimits(max_pixels=24),
            "pixel limit",
        ),
        (
            _png(suffix=b"x"),
            "image/png",
            ComputerUseFrameLimits(max_frame_bytes=24),
            "per-frame byte limit",
        ),
        (
            _png(suffix=b"x"),
            "image/png",
            ComputerUseFrameLimits(max_bytes_per_session=24),
            "per-session byte limit",
        ),
    ],
)
def test_store_frame_rejects_invalid_input_before_writing(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
    data: bytes,
    content_type: str,
    limits: ComputerUseFrameLimits,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        store_computer_use_frame(
            file_store=file_store,
            artifact_store=artifact_store,
            session_id=_SESSION,
            data=data,
            content_type=content_type,
            limits=limits,
        )

    assert (
        file_store.list(
            session_id=_SESSION,
            purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
        ).data
        == []
    )
    assert artifact_store.blobs == {}


def test_store_frame_rolls_back_row_and_partial_blob_on_write_failure(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
) -> None:
    artifact_store.fail_put = True

    with pytest.raises(RuntimeError, match="simulated write failure"):
        store_computer_use_frame(
            file_store=file_store,
            artifact_store=artifact_store,
            session_id=_SESSION,
            data=_png(),
            content_type="image/png",
        )

    assert (
        file_store.list(
            session_id=_SESSION,
            purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
        ).data
        == []
    )
    assert artifact_store.blobs == {}


def test_store_frame_prunes_to_count_and_byte_limits(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
) -> None:
    frame = _png(suffix=b"x")
    limits = ComputerUseFrameLimits(
        max_frames_per_session=2,
        max_bytes_per_session=len(frame) * 2,
    )
    attachments = [
        store_computer_use_frame(
            file_store=file_store,
            artifact_store=artifact_store,
            session_id=_SESSION,
            data=frame,
            content_type="image/png",
            limits=limits,
        )
        for _ in range(3)
    ]

    retained = file_store.list(
        session_id=_SESSION,
        purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
    ).data
    retained_ids = {stored.id for stored in retained}
    assert len(retained) == 2
    assert sum(stored.bytes for stored in retained) <= len(frame) * 2
    assert attachments[-1].file_id in retained_ids
    assert set(artifact_store.blobs) == retained_ids


def test_prune_tolerates_row_removed_by_concurrent_cleanup(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row already removed during pruning must not reject the new frame."""
    limits = ComputerUseFrameLimits(max_frames_per_session=1)
    first = store_computer_use_frame(
        file_store=file_store,
        artifact_store=artifact_store,
        session_id=_SESSION,
        data=_png(),
        content_type="image/png",
        limits=limits,
    )
    assert first.file_id is not None
    original_delete = file_store.delete

    def concurrent_delete(file_id: str, session_id: str | None = None) -> bool:
        deleted = original_delete(file_id, session_id=session_id)
        return False if file_id == first.file_id and deleted else deleted

    monkeypatch.setattr(file_store, "delete", concurrent_delete)
    second = store_computer_use_frame(
        file_store=file_store,
        artifact_store=artifact_store,
        session_id=_SESSION,
        data=_png(suffix=b"new"),
        content_type="image/png",
        limits=limits,
    )

    retained = file_store.list(
        session_id=_SESSION,
        purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
    ).data
    assert [frame.id for frame in retained] == [second.file_id]
    assert set(artifact_store.blobs) == {second.file_id}


def test_prune_failure_rolls_back_new_row_and_blob(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
) -> None:
    """A retention failure cannot leave the rejected upload hidden in storage."""
    limits = ComputerUseFrameLimits(max_frames_per_session=1)
    first = store_computer_use_frame(
        file_store=file_store,
        artifact_store=artifact_store,
        session_id=_SESSION,
        data=_png(),
        content_type="image/png",
        limits=limits,
    )
    assert first.file_id is not None
    artifact_store.fail_delete_ids.add(first.file_id)

    with pytest.raises(RuntimeError, match="simulated delete failure"):
        store_computer_use_frame(
            file_store=file_store,
            artifact_store=artifact_store,
            session_id=_SESSION,
            data=_png(suffix=b"new"),
            content_type="image/png",
            limits=limits,
        )

    retained = file_store.list(
        session_id=_SESSION,
        purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
    ).data
    assert [frame.id for frame in retained] == [first.file_id]
    assert set(artifact_store.blobs) == {first.file_id}


def test_session_cleanup_includes_hidden_frame_rows_and_blobs(
    file_store: SqlAlchemyFileStore,
    artifact_store: MemoryArtifactStore,
) -> None:
    attachment = store_computer_use_frame(
        file_store=file_store,
        artifact_store=artifact_store,
        session_id=_SESSION,
        data=_png(),
        content_type="image/png",
    )
    assert attachment.file_id is not None

    deleted_ids = file_store.delete_all_for_session(_SESSION)
    for file_id in deleted_ids:
        artifact_store.delete(file_id)

    assert attachment.file_id in deleted_ids
    assert file_store.get(attachment.file_id, session_id=_SESSION) is None
    assert not artifact_store.exists(attachment.file_id)


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ComputerUseFrameLimits(max_frames_per_session=0)


def test_limit_cannot_exceed_attachment_contract() -> None:
    with pytest.raises(ValueError, match="max_dimension cannot exceed 32768"):
        ComputerUseFrameLimits(max_dimension=32_769)
