"""Bounded storage for native-harness Computer Use preview frames."""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass

from omnigent.entities import (
    FILE_PURPOSE_COMPUTER_USE_FRAME,
    FunctionCallOutputAttachment,
)
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store import FileStore

DEFAULT_MAX_FRAME_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_FRAME_DIMENSION = 8_192
DEFAULT_MAX_FRAME_PIXELS = 32 * 1024 * 1024
DEFAULT_MAX_FRAMES_PER_SESSION = 50
DEFAULT_MAX_FRAME_BYTES_PER_SESSION = 100 * 1024 * 1024
MAX_ATTACHMENT_DIMENSION = 32_768

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class ComputerUseFrameLimits:
    """Validation and per-session retention bounds for preview frames."""

    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    max_dimension: int = DEFAULT_MAX_FRAME_DIMENSION
    max_pixels: int = DEFAULT_MAX_FRAME_PIXELS
    max_frames_per_session: int = DEFAULT_MAX_FRAMES_PER_SESSION
    max_bytes_per_session: int = DEFAULT_MAX_FRAME_BYTES_PER_SESSION

    def __post_init__(self) -> None:
        """Reject invalid programmatic limits before accepting frame bytes."""
        values = (
            self.max_frame_bytes,
            self.max_dimension,
            self.max_pixels,
            self.max_frames_per_session,
            self.max_bytes_per_session,
        )
        if any(value <= 0 for value in values):
            raise ValueError("computer frame limits must be positive")
        if self.max_dimension > MAX_ATTACHMENT_DIMENSION:
            raise ValueError(
                f"computer frame max_dimension cannot exceed {MAX_ATTACHMENT_DIMENSION}"
            )


def configured_computer_use_frame_limits() -> ComputerUseFrameLimits:
    """Load operator-configured frame limits with safe built-in defaults."""
    from omnigent.server.server_config import (
        computer_use_frame_max_bytes,
        computer_use_frame_max_count,
        computer_use_frame_max_dimension,
        computer_use_frame_max_pixels,
        computer_use_frame_max_total_bytes,
    )

    return ComputerUseFrameLimits(
        max_frame_bytes=computer_use_frame_max_bytes(),
        max_dimension=computer_use_frame_max_dimension(),
        max_pixels=computer_use_frame_max_pixels(),
        max_frames_per_session=computer_use_frame_max_count(),
        max_bytes_per_session=computer_use_frame_max_total_bytes(),
    )


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Read JPEG dimensions from a bounded marker scan."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG signature")
    offset = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset < len(data):
        while offset < len(data) and data[offset] != 0xFF:
            offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in sof_markers:
            if length < 7:
                break
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    raise ValueError("JPEG dimensions not found")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    """Read dimensions from a VP8, VP8L, or VP8X WebP header."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("invalid WebP signature")
    chunk = data[12:16]
    chunk_size = int.from_bytes(data[16:20], "little")
    payload = data[20:]
    if chunk_size > len(payload):
        raise ValueError("truncated WebP chunk")
    if chunk == b"VP8X" and chunk_size >= 10:
        width = 1 + int.from_bytes(payload[4:7], "little")
        height = 1 + int.from_bytes(payload[7:10], "little")
        return width, height
    if chunk == b"VP8 " and chunk_size >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and chunk_size >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        width = 1 + (bits & 0x3FFF)
        height = 1 + ((bits >> 14) & 0x3FFF)
        return width, height
    raise ValueError("WebP dimensions not found")


def image_dimensions(data: bytes, content_type: str) -> tuple[int, int]:
    """Validate an allowed image signature and return ``(width, height)``."""
    if content_type == "image/png":
        if (
            len(data) < 24
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or data[8:12] != b"\x00\x00\x00\r"
            or data[12:16] != b"IHDR"
        ):
            raise ValueError("invalid PNG header")
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if content_type == "image/jpeg":
        return _jpeg_dimensions(data)
    if content_type == "image/webp":
        return _webp_dimensions(data)
    raise ValueError(f"unsupported computer frame content type: {content_type!r}")


def _validate_frame(
    data: bytes,
    content_type: str,
    limits: ComputerUseFrameLimits,
) -> tuple[int, int]:
    """Validate decoded bytes, image type, dimensions, and retention fit."""
    if not data:
        raise ValueError("computer frame is empty")
    if len(data) > limits.max_frame_bytes:
        raise ValueError("computer frame exceeds per-frame byte limit")
    if len(data) > limits.max_bytes_per_session:
        raise ValueError("computer frame exceeds per-session byte limit")
    width, height = image_dimensions(data, content_type)
    if width <= 0 or height <= 0:
        raise ValueError("computer frame dimensions must be positive")
    if width > limits.max_dimension or height > limits.max_dimension:
        raise ValueError("computer frame exceeds dimension limit")
    if width * height > limits.max_pixels:
        raise ValueError("computer frame exceeds pixel limit")
    return width, height


def _prune_frames(
    *,
    file_store: FileStore,
    artifact_store: ArtifactStore,
    session_id: str,
    keep_file_id: str,
    limits: ComputerUseFrameLimits,
) -> None:
    """Delete oldest retained frames until both session limits are satisfied."""
    frames = []
    after: str | None = None
    while True:
        page = file_store.list(
            session_id=session_id,
            purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
            order="asc",
            limit=1_000,
            after=after,
        )
        frames.extend(page.data)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    total_bytes = sum(frame.bytes for frame in frames)
    remaining = len(frames)
    for frame in frames:
        within_count = remaining <= limits.max_frames_per_session
        within_bytes = total_bytes <= limits.max_bytes_per_session
        if within_count and within_bytes:
            break
        if frame.id == keep_file_id:
            continue
        artifact_store.delete(frame.id)
        # A concurrent session cleanup may have removed the row after this
        # listing. ``False`` means the row is already absent, which satisfies
        # pruning; treating it as a storage failure would reject the new frame
        # after its row and blob were already committed.
        file_store.delete(frame.id, session_id=session_id)
        remaining -= 1
        total_bytes -= frame.bytes


def store_computer_use_frame(
    *,
    file_store: FileStore,
    artifact_store: ArtifactStore,
    session_id: str,
    data: bytes,
    content_type: str,
    limits: ComputerUseFrameLimits | None = None,
    dedup_key: str | None = None,
) -> FunctionCallOutputAttachment:
    """Validate, store, and reference one hidden Computer Use preview frame.

    Metadata is rolled back if blob storage fails. After a successful write,
    oldest generated frames are removed until the configured per-session count
    and byte limits are both satisfied.
    """
    if not session_id:
        raise ValueError("computer frame requires a session_id")
    effective_limits = limits or configured_computer_use_frame_limits()
    width, height = _validate_frame(data, content_type, effective_limits)
    filename = f"computer-use-frame{_EXTENSIONS[content_type]}"
    if dedup_key is not None:
        source_digest = hashlib.sha256(dedup_key.encode("utf-8")).digest()
        digest = hashlib.sha256(source_digest + data).hexdigest()[:32]
        filename = f"computer-use-frame-{digest}{_EXTENSIONS[content_type]}"
        after: str | None = None
        while True:
            page = file_store.list(
                session_id=session_id,
                purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
                order="asc",
                limit=1_000,
                after=after,
            )
            for existing in page.data:
                if existing.filename != filename or existing.content_type != content_type:
                    continue
                if artifact_store.exists(existing.id):
                    _prune_frames(
                        file_store=file_store,
                        artifact_store=artifact_store,
                        session_id=session_id,
                        keep_file_id=existing.id,
                        limits=effective_limits,
                    )
                    return FunctionCallOutputAttachment(
                        kind="computer_frame",
                        file_id=existing.id,
                        content_type=content_type,
                        width=width,
                        height=height,
                    )
                file_store.delete(existing.id, session_id=session_id)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
    stored = file_store.create(
        filename=filename,
        bytes=len(data),
        content_type=content_type,
        session_id=session_id,
        purpose=FILE_PURPOSE_COMPUTER_USE_FRAME,
    )
    try:
        artifact_store.put(stored.id, data)
    except Exception:
        with contextlib.suppress(Exception):
            artifact_store.delete(stored.id)
        file_store.delete(stored.id, session_id=session_id)
        raise
    try:
        _prune_frames(
            file_store=file_store,
            artifact_store=artifact_store,
            session_id=session_id,
            keep_file_id=stored.id,
            limits=effective_limits,
        )
    except Exception:
        # The upload has not been returned to the caller yet, so undo it if
        # retention enforcement fails. This prevents an unreferenced hidden
        # frame from surviving a 5xx response.
        with contextlib.suppress(Exception):
            artifact_store.delete(stored.id)
        with contextlib.suppress(Exception):
            file_store.delete(stored.id, session_id=session_id)
        raise
    return FunctionCallOutputAttachment(
        kind="computer_frame",
        file_id=stored.id,
        content_type=content_type,
        width=width,
        height=height,
    )
