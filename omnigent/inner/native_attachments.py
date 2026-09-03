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
from pathlib import Path, PurePath

import httpx

_logger = logging.getLogger(__name__)

# Characters that would corrupt a "[Attached: ...]" / "[Attachment ...]"
# marker line for the consumers that regex-match it (forwarders, title
# seeding): brackets end the match early, newlines break the line shape.
_MARKER_UNSAFE = re.compile(r"[\[\]\r\n]")

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


# Subdirectory of the harness workspace that user-attached files land in.
WORKSPACE_ATTACHMENTS_DIRNAME = "session-attachments"

# Per-file upload cap and per-session disk quotas for workspace-materialized
# attachments. These bound sandbox disk usage rather than a base64 request
# payload — nothing here is ever inlined — so they sit well above the inline
# caps in omnigent/runtime/content_resolver.py. Conservative and not yet
# deployment-configurable; widen alongside a denylist-config follow-up.
MAX_WORKSPACE_ATTACHMENT_UPLOAD_BYTES: int = 50 * 1024 * 1024
MAX_SESSION_WORKSPACE_ATTACHMENTS: int = 20
MAX_SESSION_WORKSPACE_ATTACHMENT_BYTES: int = 200 * 1024 * 1024

# Extensions delivered by materializing to the workspace instead of inlining.
# Office formats and archives are zip containers that browsers and OSes
# routinely mislabel as application/zip or application/octet-stream, so the
# check is extension-based rather than content-type-based. Kept a fixed
# allowlist rather than "any binary": until deployments can configure a
# denylist, an open default would let arbitrary executables into the sandbox.
_WORKSPACE_MATERIALIZE_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".docx", ".xlsx", ".pptx", ".db", ".sqlite", ".sqlite3"}
)


def workspace_materialize_upload_limit(filename: str | None) -> int | None:
    """
    Max size (bytes) for a workspace-materialized attachment, or ``None``
    when *filename*'s extension is not one.

    These types are never inlined into the model context: a filesystem-capable
    harness (Claude Code, Codex) writes them into its workspace and references
    them by path, so adapters without a filesystem must reject them rather
    than send bytes the provider cannot interpret.

    :param filename: The original filename, e.g. ``"report.docx"``.
    :returns: :data:`MAX_WORKSPACE_ATTACHMENT_UPLOAD_BYTES`, or ``None`` when
        the extension is not a workspace-materialize type.
    """
    if not filename:
        return None
    if PurePath(filename).suffix.lower() not in _WORKSPACE_MATERIALIZE_EXTENSIONS:
        return None
    return MAX_WORKSPACE_ATTACHMENT_UPLOAD_BYTES


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
    decoded = _decode_attachment_block(block)
    if decoded is None:
        return None
    raw_bytes, filename = decoded

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


def _decode_attachment_block(block: Mapping[str, object]) -> tuple[bytes, str] | None:
    """
    Decode a block's data URI and derive a safe base filename for it.

    :param block: Attachment content block (see
        :func:`materialize_attachment`).
    :returns: ``(raw_bytes, filename)`` where *filename* carries no
        directory components and no marker-breaking characters, or ``None``
        when the block has no usable data URI.
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
        # ``.name`` drops any directory part, so "../../etc/passwd" becomes
        # "passwd" — a traversal attempt can't escape the destination dir.
        filename = Path(filename).name or f"attachment_{uuid.uuid4().hex[:8]}{ext}"
    return raw_bytes, _MARKER_UNSAFE.sub("_", filename)


def workspace_attachment_usage(workspace: Path) -> tuple[int, int]:
    """
    Count and total size of files already materialized into *workspace*.

    :param workspace: Harness workspace root, e.g. ``Path("/home/me/repo")``.
    :returns: ``(file_count, total_bytes)`` for the session-attachments
        directory. Best-effort: an unreadable or absent directory counts as
        empty, since a stat failure must not fail the turn.
    """
    count = 0
    total = 0
    try:
        for entry in (workspace / WORKSPACE_ATTACHMENTS_DIRNAME).iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            count += 1
            total += entry.stat().st_size
    except OSError:
        pass
    return count, total


def materialize_attachment_to_workspace(
    block: Mapping[str, object], workspace: Path
) -> Path | None:
    """
    Write an attachment into the harness workspace for the agent to open.

    Unlike :func:`materialize_attachment`, which stages inlinable files in
    the bridge directory, this places the file under the workspace the
    harness already runs in, so its own Read/Bash tools reach it without a
    sandbox exception. The bytes are written verbatim: archives are never
    extracted, and the execute bits are always cleared so a materialized
    file can't be run.

    :param block: Attachment content block (see
        :func:`materialize_attachment`).
    :param workspace: Harness workspace root, e.g. ``Path("/home/me/repo")``.
    :returns: Path to the written file, or ``None`` when the block could not
        be materialized — undecodable, quota exceeded, or a destination that
        escapes the attachments directory.
    """
    decoded = _decode_attachment_block(block)
    if decoded is None:
        return None
    raw_bytes, filename = decoded

    attachments_dir = workspace.resolve() / WORKSPACE_ATTACHMENTS_DIRNAME
    if attachments_dir.is_symlink():
        # A symlinked attachments dir would redirect writes out of the
        # workspace, so refuse rather than following it.
        _logger.warning("Refusing to materialize into symlinked %s", attachments_dir)
        return None

    dest = attachments_dir / filename
    try:
        dest.resolve().relative_to(attachments_dir)
    except ValueError:
        _logger.warning("Refusing attachment path outside %s", attachments_dir)
        return None
    if dest.is_symlink():
        _logger.warning("Refusing to write through symlink %s", dest)
        return None

    if dest.exists() and not _holds_bytes(dest, raw_bytes):
        # Same name, different bytes — suffix by content so one file exists
        # per payload rather than a copy per rebuild.
        digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
        dest = dest.with_stem(f"{dest.stem}_{digest}")
    if _holds_bytes(dest, raw_bytes):
        # Already materialized (e.g. a re-resolved block after a runner
        # relaunch); reuse it so the rewrite doesn't count against quota.
        return dest

    count, total = workspace_attachment_usage(workspace)
    if (
        count + 1 > MAX_SESSION_WORKSPACE_ATTACHMENTS
        or total + len(raw_bytes) > MAX_SESSION_WORKSPACE_ATTACHMENT_BYTES
    ):
        _logger.warning(
            "Session attachment quota exceeded (%d files, %d bytes); skipping %s",
            count,
            total,
            filename,
        )
        return None

    try:
        attachments_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw_bytes)
        dest.chmod(dest.stat().st_mode & ~0o111)
    except OSError:
        _logger.warning("Failed to materialize attachment to %s", dest, exc_info=True)
        return None
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
# "[Attached: <path>]" from attachment_reference_line(), the workspace
# "[Attached file: <path>]" from workspace_attachment_reference_line(), and
# the unresolved marker. TUI forwarders strip these from mirrored bubbles
# (internal bridge details that must not leak into the chat transcript).
ATTACHMENT_MARKER_STRIP_PATTERN = (
    rf"\[Attached(?: file)?:[^\]]*\]|{UNRESOLVED_ATTACHMENT_MARKER_PATTERN}"
)


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


def workspace_attachment_reference_line(block: Mapping[str, object], workspace: Path) -> str:
    """
    Materialize *block* into *workspace* and return its transcript line.

    Uses the ``"[Attached file: <path>]"`` shape codex-native already emits,
    which the harness echoes back and title seeding strips — see
    :data:`ATTACHMENT_MARKER_STRIP_PATTERN`.

    :param block: Attachment content block (see
        :func:`materialize_attachment_to_workspace`).
    :param workspace: Harness workspace root the file is written under.
    :returns: ``"[Attached file: <path>]"`` on success, else the visible
        marker from :func:`unresolved_attachment_marker`.
    """
    path = materialize_attachment_to_workspace(block, workspace)
    if path is not None:
        return f"[Attached file: {path}]"
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


async def resolve_file_id_block(
    block: Mapping[str, object],
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> dict[str, object] | None:
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
    :returns: The rebuilt block without ``file_id``, or ``None`` when the
        fetch failed — callers keep the original block so a visible
        marker can surface downstream.
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
    if block.get("type") == "input_image":
        new_block["image_url"] = f"data:{content_type};base64,{encoded}"
    else:
        new_block["file_data"] = f"data:{content_type};base64,{encoded}"
    return new_block
