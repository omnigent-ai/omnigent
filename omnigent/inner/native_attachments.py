"""
Shared helpers for materializing multimodal attachment blocks to disk.

Both native executors (Claude Code, Codex) receive user messages whose
image/file content blocks carry resolved base64 data URIs. Inlining that
base64 into the text sent to the native CLI is wrong: Claude Code cannot
view it, and the Codex app-server rejects any turn whose input text
exceeds 1 MiB (``input_too_large``). Instead each executor decodes the
data URI to a file on disk and references it by path — Claude Code via
its Read tool, Codex via a ``localImage`` input item. This module owns
that shared decode-and-write step.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

_logger = logging.getLogger(__name__)

# Characters that would corrupt a "[Attached: ...]" / "[Attachment ...]"
# marker line for the consumers that regex-match it (forwarders, title
# seeding): brackets end the match early, newlines break the line shape.
_MARKER_UNSAFE = re.compile(r"[\[\]\r\n]")
FRAMEWORK_NOTICE_BLOCK_TYPE = "_omnigent_framework_notice"
CLAUDE_FRAMEWORK_CONTEXT_FILE = "pending_framework_context.txt"
_RESIZE_NOTICE_DIMS = re.compile(r"downscaled from (\d+)×(\d+) px")

# Maps a data-URI MIME type to the file extension used when no filename
# is supplied, e.g. ``"image/png"`` -> ``".png"``.
MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}


@dataclass(frozen=True)
class DataUri:
    """
    Decoded components of a ``data:`` URI.

    :param mime_type: The MIME type, e.g. ``"image/png"``.
    :param base64_payload: The base64-encoded payload following the
        comma, e.g. ``"iVBORw0KGgo..."``.
    """

    mime_type: str
    base64_payload: str


def parse_data_uri(uri: str) -> DataUri:
    """
    Split a ``data:`` URI into its MIME type and base64 payload.

    :param uri: Data URI string,
        e.g. ``"data:image/png;base64,iVBOR..."``.
    :returns: A :class:`DataUri` with the MIME type and base64 payload.
    :raises ValueError: If the URI has no comma separating header from
        payload.
    """
    # "data:image/png;base64,iVBOR..."
    header, _, payload = uri.partition(",")
    if not payload:
        raise ValueError(f"Malformed data URI: no comma separator in {uri[:80]}")
    # header = "data:image/png;base64"
    mime_part = header.removeprefix("data:").removesuffix(";base64")
    return DataUri(mime_type=mime_part, base64_payload=payload)


def materialize_attachment(block: Mapping[str, object], bridge_dir: Path) -> Path | None:
    """
    Decode a base64 data URI from a content block and write it to disk.

    :param block: A content block dict with ``type`` of
        ``"input_image"`` or ``"input_file"``. Expected to carry a
        resolved data URI in ``image_url`` or ``file_data``,
        e.g. ``"data:image/png;base64,iVBOR..."``. May also carry a
        ``filename``, e.g. ``"diagram.png"``.
    :param bridge_dir: Bridge directory path. Files are written to an
        ``uploads/`` subdirectory underneath it,
        e.g. ``Path("/tmp/omnigent/codex-native/<digest>")``.
    :returns: Path to the written file, or ``None`` if the block could
        not be materialized (missing data URI, decode error).
    """
    data_uri = block.get("image_url") or block.get("file_data")
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        if block.get("file_id"):
            _logger.error(
                "Native executor received unresolved file_id %s — "
                "content resolver may not have run",
                block["file_id"],
            )
        return None

    try:
        parsed = parse_data_uri(data_uri)
        raw_bytes = base64.b64decode(parsed.base64_payload)
    except (ValueError, binascii.Error):
        _logger.warning("Failed to decode data URI for attachment", exc_info=True)
        return None

    ext = MIME_TO_EXT.get(parsed.mime_type, "")
    filename = block.get("filename")
    if not isinstance(filename, str) or not filename:
        filename = f"attachment_{uuid.uuid4().hex[:8]}{ext}"
    else:
        filename = Path(filename).name or f"attachment_{uuid.uuid4().hex[:8]}{ext}"
    filename = _MARKER_UNSAFE.sub("_", filename)

    uploads_dir = bridge_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / filename
    if dest.exists() and not _holds_bytes(dest, raw_bytes):
        # Same name, different bytes. Deriving the suffix from the content keeps
        # one file per payload, where a random one grew a copy per rebuild.
        digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
        dest = dest.with_stem(f"{dest.stem}_{digest}")
    if not _holds_bytes(dest, raw_bytes):
        dest.write_bytes(raw_bytes)
    return dest


def _holds_bytes(path: Path, raw_bytes: bytes) -> bool:
    """
    True if *path* already holds exactly *raw_bytes*.

    :param path: Candidate destination that may or may not exist.
    :param raw_bytes: Decoded attachment payload.
    :returns: Whether the existing file can be reused as-is. The size
        check short-circuits the read for the common mismatch.
    """
    if not path.exists():
        return False
    return path.stat().st_size == len(raw_bytes) and path.read_bytes() == raw_bytes


# Regex source matching the exact line unresolved_attachment_marker() emits.
# Consumers (title synthesis, TUI forwarders) compose their marker-matching
# patterns from this so the shapes cannot drift apart.
UNRESOLVED_ATTACHMENT_MARKER_PATTERN = r"\[Attachment [^\]]+ could not be loaded\]"

# Matches any attachment reference line this module emits — the success-path
# "[Attached: <path>]" from attachment_reference_line() and the unresolved
# marker. TUI forwarders strip these from mirrored bubbles (internal bridge
# details that must not leak into the chat transcript).
ATTACHMENT_MARKER_STRIP_PATTERN = rf"\[Attached:[^\]]*\]|{UNRESOLVED_ATTACHMENT_MARKER_PATTERN}"


def unresolved_attachment_marker(block: Mapping[str, object]) -> str:
    """
    Visible placeholder for an attachment that could not be loaded.

    Callers emit this in place of the usual path reference when
    :func:`materialize_attachment` fails, so the model (and the mirrored
    transcript) sees that an attachment was lost instead of silently
    receiving nothing and hallucinating its content.

    :param block: The content block that failed to materialize. Named by
        its ``filename``, falling back to ``file_id`` then ``"attachment"``,
        with marker-breaking characters replaced by ``_``.
    :returns: Marker line, e.g.
        ``"[Attachment photo.png could not be loaded]"``. Always matches
        :data:`UNRESOLVED_ATTACHMENT_MARKER_PATTERN`.
    """
    name = str(block.get("filename") or block.get("file_id") or "attachment")
    return f"[Attachment {_MARKER_UNSAFE.sub('_', name)} could not be loaded]"


def attachment_reference_line(block: Mapping[str, object], bridge_dir: Path) -> str:
    """
    Materialize *block* and return the transcript line referencing it.

    The line shape is load-bearing: TUI forwarders and title seeding
    (``omnigent/entities/conversation.py``) match it via
    :data:`ATTACHMENT_MARKER_STRIP_PATTERN`.

    :param block: Attachment content block (see
        :func:`materialize_attachment`).
    :param bridge_dir: Bridge directory the file is written under.
    :returns: ``"[Attached: <path>]"`` on success, else the visible
        marker from :func:`unresolved_attachment_marker`.
    """
    path = materialize_attachment(block, bridge_dir)
    if path is not None:
        return f"[Attached: {path}]"
    return unresolved_attachment_marker(block)


def has_unresolved_file_id(block: Mapping[str, object]) -> bool:
    """
    True if *block* carries a ``file_id`` no resolver has inlined yet.

    :param block: Message content block dict.
    :returns: Whether the block still needs :func:`resolve_file_id_block`.
    """
    file_id = block.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return False
    data_uri = block.get("image_url") or block.get("file_data")
    return not (isinstance(data_uri, str) and data_uri.startswith("data:"))


def resize_notice(source_metadata: Mapping[str, object] | None) -> str | None:
    """
    Model-facing note that an uploaded image was downscaled, or ``None``.

    The single source of truth for the resize-notice wording, shared by
    every attachment-resolution path (the in-process resolver in
    ``omnigent.runtime.content_resolver`` and the runner/native-harness
    resolver in :func:`resolve_file_id_block`) so the two never drift.

    :param source_metadata: A stored file's ``source_metadata`` dict (see
        :class:`omnigent.entities.file.StoredFile`). A downscaled image
        carries the pre-downscale ``width`` / ``height``.
    :returns: The notice text when the image was downscaled, else ``None``
        (passthrough images, non-images, or absent metadata).
    """
    if not source_metadata:
        return None
    width = source_metadata.get("width")
    height = source_metadata.get("height")
    if not width or not height:
        return None
    return (
        f"Note: the attached image was downscaled from {width}×{height} px to fit "
        "size limits, so you are viewing a lower-resolution version. Ask the user "
        "for a crop of the original if you need finer detail — re-uploading the whole "
        "image would be downscaled the same way."
    )


def framework_notice_block(text: str) -> dict[str, object]:
    """Build transient model context that must not become user text."""
    return {"type": FRAMEWORK_NOTICE_BLOCK_TYPE, "text": text}


def framework_notices(content: object) -> list[str]:
    """Extract transient framework notices from structured content."""
    if not isinstance(content, list):
        return []
    notices: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != FRAMEWORK_NOTICE_BLOCK_TYPE:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            notices.append(text)
    return notices


def codex_resize_metadata_path(path: Path, notice: str) -> Path:
    """Encode resize metadata in Codex's model-visible image path."""
    match = _RESIZE_NOTICE_DIMS.search(notice)
    if match is None:
        return path
    width, height = match.groups()
    alias = path.with_name(
        f"{path.stem}__omnigent-downscaled-from-{width}x{height}"
        f"-request-crop-for-fine-detail{path.suffix}"
    )
    try:
        path.replace(alias)
    except OSError:
        _logger.warning("Failed to add resize metadata to Codex image path", exc_info=True)
        return path
    return alias


async def resolve_file_id_block(
    block: Mapping[str, object],
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> tuple[dict[str, object], str | None] | None:
    """
    Fetch a ``file_id`` attachment's bytes and inline them as a data URI.

    Used wherever message content must be consumed away from the server's
    file store (the out-of-process runner, transcript rebuilds): the bytes
    are fetched back through the session-scoped file resource endpoints
    and inlined under ``image_url`` (images) or ``file_data`` (other
    files).

    :param block: Content block for which :func:`has_unresolved_file_id`
        is true.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param client: HTTP client pointed at the Omnigent server.
    :returns: ``(rebuilt_block, notice)`` — the block without ``file_id``,
        and a resize notice (see :func:`resize_notice`) to emit as a
        sibling text block when the image was downscaled, else ``None``.
        Returns ``None`` (not a tuple) when the fetch failed, so callers
        keep the original block and a visible marker can surface downstream.
    """
    file_id = str(block.get("file_id"))
    base = (
        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}"
        f"/resources/files/{urllib.parse.quote(file_id, safe='')}"
    )
    try:
        meta_resp = await client.get(base, timeout=10.0)
        content_resp = await client.get(f"{base}/content", timeout=30.0)
        meta_resp.raise_for_status()
        content_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "failed to resolve file_id=%s for session=%s",
            file_id,
            session_id,
            exc_info=True,
        )
        return None

    try:
        parsed = meta_resp.json() if meta_resp.content else {}
    except ValueError:
        parsed = None
    meta = parsed if isinstance(parsed, dict) else {}
    if meta_resp.content and not meta:
        # Unusable metadata only costs the media-type hint; the content
        # response's Content-Type header still provides it.
        _logger.warning(
            "unusable file metadata for file_id=%s in session=%s; "
            "falling back to the content headers",
            file_id,
            session_id,
        )
    content_type = meta.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        content_type = content_resp.headers.get("content-type") or "application/octet-stream"
    # Strip any charset suffix: data URIs need the media type hint.
    content_type = content_type.split(";", 1)[0]
    encoded = base64.b64encode(content_resp.content).decode("ascii")
    new_block = {k: v for k, v in block.items() if k != "file_id"}
    notice: str | None = None
    if block.get("type") == "input_image":
        new_block["image_url"] = f"data:{content_type};base64,{encoded}"
        source_metadata = meta.get("source_metadata")
        if isinstance(source_metadata, Mapping):
            notice = resize_notice(source_metadata)
    else:
        new_block["file_data"] = f"data:{content_type};base64,{encoded}"
    return new_block, notice
