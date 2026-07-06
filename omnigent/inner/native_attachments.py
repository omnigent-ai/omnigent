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
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

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


def materialize_attachment(block: dict[str, Any], bridge_dir: Path) -> Path | None:
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
    except (ValueError, base64.binascii.Error):
        _logger.warning("Failed to decode data URI for attachment", exc_info=True)
        return None

    ext = MIME_TO_EXT.get(parsed.mime_type, "")
    filename = block.get("filename")
    if not filename:
        filename = f"attachment_{uuid.uuid4().hex[:8]}{ext}"
    else:
        filename = Path(filename).name or f"attachment_{uuid.uuid4().hex[:8]}{ext}"

    uploads_dir = bridge_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / filename
    if dest.exists():
        # Same bytes already on disk (e.g. the same history re-materialized
        # on every resume) — reuse it instead of accumulating copies.
        if dest.read_bytes() == raw_bytes:
            return dest
        stem = dest.stem
        dest = uploads_dir / f"{stem}_{uuid.uuid4().hex[:6]}{dest.suffix}"

    dest.write_bytes(raw_bytes)
    return dest


# Regex source matching the exact line unresolved_attachment_marker() emits.
# Consumers (title synthesis, TUI forwarders) compose their marker-matching
# patterns from this so the shapes cannot drift apart.
UNRESOLVED_ATTACHMENT_MARKER_PATTERN = r"\[Attachment [^\]]+ could not be loaded\]"


def attachment_marker_name(block: dict[str, Any]) -> str:
    """
    Display name for an attachment block, safe inside a marker line.

    :param block: Content block; named by its ``filename``, falling back
        to ``file_id`` then ``"attachment"``.
    :returns: The name with ``[``, ``]``, and newlines replaced by ``_``
        so a marker built from it always matches the consumer regexes,
        e.g. ``"shot _final_.png"`` for ``"shot [final].png"``.
    """
    name = str(block.get("filename") or block.get("file_id") or "attachment")
    return re.sub(r"[\[\]\r\n]", "_", name)


def unresolved_attachment_marker(block: dict[str, Any]) -> str:
    """
    Visible placeholder for an attachment that could not be loaded.

    Callers emit this in place of the usual path reference when
    :func:`materialize_attachment` fails, so the model (and the mirrored
    transcript) sees that an attachment was lost instead of silently
    receiving nothing and hallucinating its content.

    :param block: The content block that failed to materialize. Named
        via :func:`attachment_marker_name`, e.g. ``"photo.png"``.
    :returns: Marker line, e.g.
        ``"[Attachment photo.png could not be loaded]"``. Always matches
        :data:`UNRESOLVED_ATTACHMENT_MARKER_PATTERN`.
    """
    return f"[Attachment {attachment_marker_name(block)} could not be loaded]"
