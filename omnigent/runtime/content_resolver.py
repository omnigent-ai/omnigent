"""Resolve file_id references in content blocks to inline content.

Scans conversation items for content blocks that reference uploaded
files via ``file_id`` and replaces them with inline base64 content.
This runs as a pre-processing step before prompt construction so the
prompt builder remains pure (no I/O).

See ``designs/MULTIMODAL_INFERENCE.md`` for the full design.
"""

from __future__ import annotations

import base64
import copy
import logging
from typing import Any

from omnigent.entities import ConversationItem, MessageData
from omnigent.stores import ArtifactStore, FileStore

_logger = logging.getLogger(__name__)

# Extensions that Python's mimetypes module doesn't always know,
# depending on the platform and Python version. Used as a fallback
# when the stored content_type is missing or generic. LLM providers
# (OpenAI) reject application/octet-stream for text files, so any
# text-like format needs a proper MIME type.
# MIME types the OpenAI Responses API accepts on
# ``input_file.file_data`` data URIs. Text-like types outside this
# allowlist (e.g. ``text/yaml``, ``text/x-rust``, ``text/typescript``)
# get rejected at the provider with a 400 ``invalid_value`` referencing
# ``input[N].content[M].file_data``. The accompanying ``filename``
# already tells the model the original extension, so collapsing to
# ``text/plain`` loses no signal — see :func:`_safe_file_data_mime`.
_FILE_DATA_PASSTHROUGH_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "application/json",
        "text/javascript",
        "application/javascript",
        "text/x-python",
    }
)


_EXTRA_MIME_TYPES: dict[str, str] = {
    # Markup / config
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "text/plain",
    ".jsonl": "application/jsonl",
    ".ndjson": "application/x-ndjson",
    ".proto": "text/plain",
    ".graphql": "text/plain",
    ".gql": "text/plain",
    # Languages mimetypes misses
    ".rs": "text/x-rust",
    ".go": "text/x-go",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".swift": "text/x-swift",
    ".kt": "text/x-kotlin",
    ".scala": "text/x-scala",
    ".r": "text/x-r",
    ".jl": "text/x-julia",
    ".lua": "text/x-lua",
    ".ex": "text/x-elixir",
    ".exs": "text/x-elixir",
    ".erl": "text/x-erlang",
    ".hs": "text/x-haskell",
    ".clj": "text/x-clojure",
    ".dart": "text/x-dart",
    ".vue": "text/plain",
    ".svelte": "text/plain",
    # Infra / build
    ".tf": "text/plain",
    ".hcl": "text/plain",
    ".dockerfile": "text/plain",
    ".gradle": "text/plain",
    ".ipynb": "application/x-ipynb+json",
    # Dotfiles
    ".env": "text/plain",
    ".lock": "text/plain",
}


# ── Attachment upload limits ──────────────────────────────────────────
# Uploaded attachments are inlined into the model context as base64 (see
# :func:`resolve_content_references`) and re-sent every turn, so sizes are
# bounded well under the model's context budget and the provider's API
# limits — Anthropic accepts images up to ~5 MB, PDFs up to ~32 MB / 100
# pages, and ~32 MB per request total. The per-type caps below keep a
# single attachment usable across a multi-turn conversation; the global
# ceiling backstops the total request size after base64 inflation (~1.33x).
#
# Images are the exception: we accept a much larger upload (screenshots,
# retina captures) and shrink it under the provider's per-image limit at
# upload time via :func:`compress_image_attachment`, so the stored blob —
# and the base64 re-sent every turn — always fits. The compressed result is
# what lands in the file store (see the ``files`` upload route).
# Mirrored client-side in web/src/lib/attachments.ts — keep in sync.
MAX_IMAGE_UPLOAD_BYTES: int = 50 * 1024 * 1024
MAX_PDF_UPLOAD_BYTES: int = 20 * 1024 * 1024
MAX_TEXT_UPLOAD_BYTES: int = 10 * 1024 * 1024
MAX_ATTACHMENT_UPLOAD_BYTES: int = 50 * 1024 * 1024

# The larger image cap only applies to raster formats we can actually shrink
# (see _COMPRESSIBLE_IMAGE_MIMES). Other image types — SVG and any vector /
# exotic format Pillow can't re-encode for us — keep this conservative cap and
# skip compression: raising their cap would let an oversized one slip through
# to the provider uncompressed. This is the pre-compression image limit.
IMAGE_UNCOMPRESSED_UPLOAD_BYTES: int = 5 * 1024 * 1024

# Raster image MIMEs compress_image_attachment can decode and re-encode. Only
# these get MAX_IMAGE_UPLOAD_BYTES; everything else image/* is capped at
# IMAGE_UNCOMPRESSED_UPLOAD_BYTES and passed through untouched.
_COMPRESSIBLE_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

# Pillow format names that legitimately back each compressible MIME. Used to
# reject a spoofed extension (e.g. a TIFF/BMP labeled image/png) before we hand
# 50 MB of untrusted bytes to an unintended decoder. Multi-picture JPEG (MPO)
# is deliberately excluded: it reports n_frames >= 2, so admitting it would only
# route it to the animation rejection with a confusing message — asking for a
# plain JPEG here is clearer.
_ALLOWED_PIL_FORMATS: dict[str, frozenset[str]] = {
    "image/png": frozenset({"PNG"}),
    "image/jpeg": frozenset({"JPEG"}),
    "image/webp": frozenset({"WEBP"}),
    "image/gif": frozenset({"GIF"}),
}

# Budget for an image's RAW stored bytes after compression. The stored blob is
# re-encoded as a base64 data URI on every turn (see _resolve_file_id_block),
# which inflates it ~4/3, and providers apply the ~5 MB per-image ceiling to
# that encoded payload. So the raw budget is 5 MB / (4/3) with headroom for the
# data-URI wrapper: 3.5 MB raw → ~4.7 MB encoded, safely under 5 MB. A decodable
# still image always ends up at or below the raw budget.
IMAGE_MODEL_BUDGET_BYTES: int = 3_500_000

# Longest-edge cap applied before the quality search when re-encoding an
# oversized image. Matches the provider's documented 8000 px max edge (an image
# already under this is left at native resolution); anything larger is scaled
# down to this first.
IMAGE_MAX_EDGE_PX: int = 8000

# Decompression-bomb guard: refuse to decode images whose pixel area is
# implausibly large for a real screenshot/photo.
IMAGE_MAX_DECODED_PIXELS: int = 64 * 1024 * 1024

# Copy-at-spawn limits (see the ``files:copy`` endpoint). A parent forwarding
# files to a subagent copies them through the server, which reads each source
# blob to re-store it under the child. Bounding the count and the summed
# ``StoredFile.bytes`` — checked against metadata BEFORE any blob is read —
# stops a single send from spiking shared-server memory. Defaults are the
# floor; a deployment can raise or lower them via ``server_config`` (see
# :func:`omnigent.server.server_config.copy_file_count_limit` and
# :func:`~omnigent.server.server_config.copy_total_bytes_limit`). For
# reference, OpenAI caps code-interpreter at 20 files, Anthropic Files at
# 500 MB/file.
MAX_COPY_FILES: int = 20
MAX_COPY_TOTAL_BYTES: int = 256 * 1024 * 1024

# ``application/*`` MIME types we treat as text-like. The rest of the
# text-like surface is ``text/*`` (covered by the prefix check) — these
# are the text-bearing ``application/*`` types code/data files resolve to.
_TEXT_LIKE_APPLICATION_MIMES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/jsonl",
        "application/x-ndjson",
        "application/x-ipynb+json",
    }
)


def attachment_upload_limit(content_type: str) -> int | None:
    """
    Max upload size (bytes) for *content_type*, or ``None`` if the type is
    not an allowed attachment.

    Allowed: images, PDF, and text-like files (``text/*`` plus a few
    text-bearing ``application/*`` types — JSON, JS, JSONL, notebooks).
    Office / binary formats (pptx, docx, xlsx, zip, …) return ``None`` and
    are rejected at upload: the model can't read their raw bytes
    (Anthropic's base64 ``document`` source accepts only PDF), so inlining
    them only produces garbled UTF-8 or — for large files — an oversized,
    context-blowing request. Callers reject ``None`` with HTTP 415.

    Compressible raster images (PNG, JPEG, WebP, GIF) get the large
    :data:`MAX_IMAGE_UPLOAD_BYTES` cap because an oversized one is shrunk
    under the model budget at upload; other image types (SVG, …) keep the
    smaller :data:`IMAGE_UNCOMPRESSED_UPLOAD_BYTES` cap since we can't shrink
    them.

    :param content_type: The resolved MIME type, e.g. ``"image/png"``.
        Use :func:`_resolve_content_type` to derive it from the upload's
        declared type + filename first.
    :returns: The per-type byte limit (still subject to
        :data:`MAX_ATTACHMENT_UPLOAD_BYTES`), or ``None`` when the type is
        not an allowed attachment.
    """
    if content_type.startswith("image/"):
        if content_type in _COMPRESSIBLE_IMAGE_MIMES:
            return MAX_IMAGE_UPLOAD_BYTES
        return IMAGE_UNCOMPRESSED_UPLOAD_BYTES
    if content_type == "application/pdf":
        return MAX_PDF_UPLOAD_BYTES
    if content_type.startswith("text/") or content_type in _TEXT_LIKE_APPLICATION_MIMES:
        return MAX_TEXT_UPLOAD_BYTES
    return None


class ImageCompressionError(ValueError):
    """An image attachment could not be shrunk under the model size budget.

    Raised when the bytes don't decode as an image, or when even the smallest
    downscale/quality still exceeds :data:`IMAGE_MODEL_BUDGET_BYTES`. Callers
    reject it with HTTP 413.
    """


# Filename extension for each image type we emit from compression, so a
# re-encoded attachment's name matches its bytes.
_IMAGE_CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def image_filename_for_content_type(filename: str, content_type: str) -> str:
    """Return *filename* with an extension matching *content_type*.

    :func:`compress_image_attachment` may re-encode an image (e.g. PNG →
    JPEG), leaving the original name's extension inconsistent with the stored
    bytes. Swapping it keeps name, bytes, and MIME aligned. An unknown image
    type, or a name already carrying the right extension, is returned
    unchanged.

    :param filename: The original upload filename, e.g. ``"shot.png"``.
    :param content_type: The re-encoded image MIME, e.g. ``"image/jpeg"``.
    :returns: The filename with the matching extension, e.g. ``"shot.jpg"``.
    """
    from pathlib import PurePosixPath

    target = _IMAGE_CONTENT_TYPE_EXTENSIONS.get(content_type)
    if target is None:
        return filename
    path = PurePosixPath(filename)
    if path.suffix.lower() == target:
        return filename
    try:
        return str(path.with_suffix(target))
    except ValueError:
        # Names like "." / ".." have no stem for with_suffix; keep as-is.
        return filename


def _encode_image(image: Any, image_format: str, **params: Any) -> bytes:
    """Encode *image* to *image_format*, returning the bytes."""
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format=image_format, **params)
    return buffer.getvalue()


def compress_image_attachment(content: bytes, content_type: str) -> tuple[bytes, str]:
    """Shrink a raster image so its bytes fit :data:`IMAGE_MODEL_BUDGET_BYTES`.

    Compressible raster images upload at the larger
    :data:`MAX_IMAGE_UPLOAD_BYTES`, but every attachment is inlined as base64
    into each turn's request, so a big image would blow past the provider's
    per-image limit. This downscales and/or re-encodes the image to fit the
    model budget, keeping it a valid in-place attachment (the compressed bytes
    are what get stored).

    Already-small images (``<=`` budget) and non-raster types we don't compress
    (SVG, …) are returned unchanged. For still images we search largest-first:
    alpha images try lossy then lossless WebP then PNG; opaque images try
    descending-quality JPEG, downscaling the canvas when quality alone isn't
    enough. An **animated** image over the budget is rejected: we can't
    re-encode animation, and storing it uncompressed would just fail at the
    provider at turn time.

    :param content: Raw uploaded image bytes.
    :param content_type: The resolved image MIME (``image/*``).
    :returns: ``(bytes, content_type)`` — the (possibly re-encoded) image and
        its MIME, with the bytes ``<=`` the budget; the content_type may change
        (e.g. ``image/png`` → ``image/jpeg``). A high-entropy image that can't
        reach the budget even at the smallest scale/quality raises instead.
    :raises ImageCompressionError: If the bytes don't decode as an image, are
        an oversized animation, or can't be brought under the budget. The
        message is safe to surface to the client (no raw decoder text).
    """
    # Small enough already, or a type we don't compress (SVG etc.): leave as-is.
    if len(content) <= IMAGE_MODEL_BUDGET_BYTES or content_type not in _COMPRESSIBLE_IMAGE_MIMES:
        return content, content_type

    from io import BytesIO

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(content)) as probe:
            # Reject a spoofed extension before decoding: the real container
            # (probe.format, from the header) must match the declared MIME,
            # so a TIFF/BMP-as-png can't select an unintended 50 MB decoder.
            if probe.format not in _ALLOWED_PIL_FORMATS.get(content_type, frozenset()):
                raise ImageCompressionError(
                    "this image format can't be resized; upload a PNG, JPEG, WebP, or GIF"
                )
            # Cheap header dimensions first — bail before walking frames
            # (n_frames enumerates every GIF frame). This deterministic area
            # check is the decompression-bomb guard (fires below Pillow's own
            # ~89 MP warning), so no warnings.catch_warnings() is needed.
            if probe.width * probe.height > IMAGE_MAX_DECODED_PIXELS:
                raise ImageCompressionError("the image's dimensions are too large to process")
            n_frames = int(getattr(probe, "n_frames", 1))
        # Animation can't be re-encoded here, and it's over budget, so it
        # would be rejected by the provider at turn time — reject cleanly now.
        if n_frames != 1:
            raise ImageCompressionError(
                "this animated image is too large to attach; upload a smaller "
                "or static image instead"
            )
        with Image.open(BytesIO(content)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except ImageCompressionError:
        raise
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ImageCompressionError("the file is not a readable image") from exc

    # Detect alpha broadly: RGBA/LA modes, and RGB/L/P images carrying a tRNS
    # chunk (Pillow exposes it as image.info["transparency"] without changing
    # the mode) — routing them through the alpha branch avoids flattening
    # transparency to JPEG.
    has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
    if has_alpha:
        base = image.convert("RGBA")
        # Alpha-preserving encoders, best-compression first. Generated lazily so
        # the search stops at the first candidate that fits.
        _encodings: tuple[tuple[str, str, dict[str, Any]], ...] = (
            ("WEBP", "image/webp", {"quality": 80, "method": 4}),
            ("WEBP", "image/webp", {"quality": 60, "method": 4}),
            ("PNG", "image/png", {"optimize": True}),
        )
    else:
        base = image.convert("RGB")
        _encodings = tuple(
            ("JPEG", "image/jpeg", {"quality": q, "optimize": True, "progressive": True})
            for q in (85, 70, 55)
        )

    # Pre-shrink an oversized canvas to the edge cap before the quality search.
    longest = max(base.width, base.height)
    if longest > IMAGE_MAX_EDGE_PX:
        factor = IMAGE_MAX_EDGE_PX / longest
        base = base.resize(
            (max(1, round(base.width * factor)), max(1, round(base.height * factor))),
            Image.Resampling.LANCZOS,
        )

    # Largest-first over a small scale/quality grid (≤ 3 scales × 3 encodings),
    # encoding one candidate at a time and returning at the first that fits, so a
    # worst-case (incompressible) upload can't fan out into many eager encodes.
    try:
        for scale in (1.0, 0.5, 0.25):
            if scale == 1.0:
                frame = base
            else:
                frame = base.resize(
                    (max(1, round(base.width * scale)), max(1, round(base.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            for image_format, mime, params in _encodings:
                data = _encode_image(frame, image_format, **params)
                if len(data) <= IMAGE_MODEL_BUDGET_BYTES:
                    return data, mime
    except (OSError, ValueError) as exc:
        # A Pillow encode error becomes a clean 413, not an unhandled 500.
        raise ImageCompressionError("the image couldn't be re-encoded") from exc

    raise ImageCompressionError("the image couldn't be compressed to a supported size")


# Extensions accepted as text/code attachments even when the upload's
# declared MIME mislabels them as binary — e.g. a ``.csv`` tagged
# ``application/vnd.ms-excel`` on Windows, or a ``.ts`` tagged
# ``video/mp2t``. Mirrors TEXT_CODE_EXTENSIONS in
# web/src/lib/attachments.ts — keep in sync.
_TEXT_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".log",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".env",
        ".lock",
        ".proto",
        ".graphql",
        ".gql",
        ".html",
        ".htm",
        ".xml",
        ".css",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".scala",
        ".swift",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".php",
        ".pl",
        ".r",
        ".jl",
        ".lua",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".clj",
        ".dart",
        ".vue",
        ".svelte",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".sql",
        ".tf",
        ".hcl",
        ".gradle",
        ".dockerfile",
        ".ipynb",
    }
)


def attachment_text_type_for_extension(filename: str | None) -> str | None:
    """
    Resolve a text-like MIME for *filename* by extension, or ``None``.

    Used as a fallback when the upload's declared MIME mislabels a text/code
    file as binary (e.g. a ``.csv`` reported as ``application/vnd.ms-excel``):
    only extensions in :data:`_TEXT_CODE_EXTENSIONS` are honored, so a real
    binary (``.xls``, ``.pptx``) is never re-admitted. Mirrors the web
    client's extension allowlist so the two agree on what's attachable.

    :param filename: The original filename, e.g. ``"data.csv"``.
    :returns: A concrete text-like MIME (e.g. ``"text/csv"``), or ``None``
        when the extension is not a recognised text/code type.
    """
    import mimetypes as _mt
    from pathlib import PurePath

    if not filename:
        return None
    suffix = PurePath(filename).suffix.lower()
    if suffix not in _TEXT_CODE_EXTENSIONS:
        return None
    mapped = _EXTRA_MIME_TYPES.get(suffix)
    if mapped:
        return mapped
    guessed = _mt.guess_type(filename)[0]
    if guessed and (guessed.startswith("text/") or guessed in _TEXT_LIKE_APPLICATION_MIMES):
        return guessed
    return "text/plain"


# ── Text-attachment extraction for input-phase policy scanning ─────────


def _is_text_like_attachment(content_type: str, filename: str | None) -> bool:
    """Whether an attachment's content is text the policy layer can scan.

    :param content_type: Resolved MIME, e.g. ``"text/csv"``.
    :param filename: Original filename, used as an extension fallback.
    :returns: ``True`` for ``text/*`` and known text-bearing
        ``application/*`` types (or a text-like extension).
    """
    if content_type.startswith("text/"):
        return True
    if content_type in _TEXT_LIKE_APPLICATION_MIMES:
        return True
    return attachment_text_type_for_extension(filename) is not None


def extract_text_attachments(
    content: list[dict[str, Any]],
    file_store: FileStore,
    artifact_store: ArtifactStore,
    *,
    session_id: str | None = None,
) -> list[dict[str, str]]:
    """Decode text-like ``input_file`` attachments into structured entries.

    Used by the request-phase policy gate so that PII (and other) policies
    scan the *content* of an attached text file — not just the typed
    message. Attachments arrive as ``input_file`` blocks that are base64-
    inlined straight to the model (see :func:`resolve_content_references`),
    so without this an attached CSV of card numbers reaches the LLM
    unscanned. Non-text attachments (images, PDFs, binaries) are skipped;
    text files are decoded in full (uploads are already bounded — text ≤
    :data:`MAX_TEXT_UPLOAD_BYTES`, 10 MB). Best-effort: a missing/foreign file
    or a fetch error is skipped, never raised, so a scan failure can't break
    message delivery.

    :param content: The message's content blocks (``body.data["content"]``).
    :param file_store: Store for file metadata (``content_type`` / ``filename``).
    :param artifact_store: Store for the file's binary content.
    :param session_id: Owning session id, to enforce file ownership.
    :returns: A list of ``{"filename", "content_type", "text"}`` entries — one
        per scannable text attachment, in order — or ``[]`` when there are none.
    """
    attachments: list[dict[str, str]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "input_file":
            continue
        file_id = block.get("file_id")
        if not isinstance(file_id, str):
            continue
        try:
            file_meta = file_store.get(file_id)
        except Exception:  # best-effort scan; never break message delivery
            continue
        if file_meta is None:
            continue
        if (
            file_meta.session_id is not None
            and session_id is not None
            and file_meta.session_id != session_id
        ):
            continue
        content_type = _resolve_content_type(file_meta.content_type, file_meta.filename)
        if not _is_text_like_attachment(content_type, file_meta.filename):
            continue
        try:
            raw = artifact_store.get(file_id)
        except Exception:  # best-effort scan; never break message delivery
            continue
        if not raw:
            continue
        attachments.append(
            {
                "filename": file_meta.filename or "",
                "content_type": content_type,
                "text": raw.decode("utf-8", errors="replace"),
            }
        )
    return attachments


def resolve_content_references(
    items: list[ConversationItem],
    file_store: FileStore,
    artifact_store: ArtifactStore,
    cache: dict[str, str] | None = None,
    *,
    session_id: str | None = None,
) -> list[ConversationItem]:
    """
    Resolve ``file_id`` references in content blocks to inline content.

    Returns **copies** of items whose content was modified. Items
    without ``file_id`` references are returned as-is (no copy).
    The originals in the conversation store remain unchanged.

    Resolves ``file_id`` on **any** block type (``input_image``,
    ``input_file``, or future types like ``input_audio``). External
    URLs (``image_url``, ``file_url``) are never fetched — they pass
    through unchanged (SSRF protection).

    :param items: Persisted conversation items in chronological
        order, e.g. from ``conversation_store.fetch_all()``.
    :param file_store: Store for looking up file metadata
        (``content_type``, ``filename``).
    :param artifact_store: Store for fetching file binary content.
    :param cache: Optional per-task cache mapping ``file_id`` to
        its base64-encoded content. Avoids re-fetching and
        re-encoding the same file across agent loop iterations.
        Pass ``None`` to disable caching.
    :param session_id: Optional owning session id used to verify
        session-scoped file ownership, e.g. ``"conv_abc123"``.
    :returns: A list of conversation items with all ``file_id``
        references replaced by inline base64 content.
    :raises ValueError: If a referenced ``file_id`` does not exist
        in the file store.
    :raises KeyError: If a referenced ``file_id`` exists in the
        file store but its binary content is missing from the
        artifact store.
    """
    result: list[ConversationItem] = []
    for item in items:
        if item.type == "message" and isinstance(item.data, MessageData):
            resolved_content = _resolve_message_content(
                item.data.content,
                file_store,
                artifact_store,
                cache,
                session_id=session_id,
            )
            if resolved_content is item.data.content:
                # No file_id references found — reuse original.
                result.append(item)
            else:
                # Content was modified — deep-copy and replace.
                item_copy = copy.deepcopy(item)
                assert isinstance(item_copy.data, MessageData)
                item_copy.data.content = resolved_content
                result.append(item_copy)
        else:
            result.append(item)
    return result


def _resolve_message_content(
    content: list[dict[str, Any]],
    file_store: FileStore,
    artifact_store: ArtifactStore,
    cache: dict[str, str] | None = None,
    *,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Resolve ``file_id`` references in a list of content blocks.

    Returns the **original list** if no blocks contain ``file_id``
    (caller uses identity check to detect changes). Returns a
    **new list** with resolved blocks if any ``file_id`` was found.

    :param content: Content block dicts from ``MessageData.content``.
    :param file_store: Store for file metadata lookups.
    :param artifact_store: Store for binary content fetches.
    :param cache: Optional per-task base64 cache (see
        :func:`resolve_content_references`).
    :param session_id: Optional owning session id used to verify
        session-scoped file ownership, e.g. ``"conv_abc123"``.
    :returns: The original list (unchanged) or a new list with
        ``file_id`` references resolved to inline content.
    """
    resolved: list[dict[str, Any]] = []
    changed = False
    for block in content:
        if "file_id" in block:
            resolved.append(
                _resolve_file_id_block(
                    block,
                    file_store,
                    artifact_store,
                    cache,
                    session_id=session_id,
                )
            )
            changed = True
        else:
            resolved.append(block)
    # Return original list when nothing changed so caller can use
    # identity check (``is``) to skip unnecessary deep-copies.
    return resolved if changed else content


def _session_id_from_block(block: dict[str, Any]) -> str | None:
    """
    Extract optional session ownership from a content block.

    :param block: Content block dict, e.g.
        ``{"file_id": "file_abc123", "session_id": "conv_abc123"}``.
    :returns: Session id if present, otherwise ``None``.
    """
    for key in ("session_id", "conversation_id"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_file_id_block(
    block: dict[str, Any],
    file_store: FileStore,
    artifact_store: ArtifactStore,
    cache: dict[str, str] | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Resolve a single content block's ``file_id`` to inline content.

    For ``input_image`` blocks: replaces ``file_id`` with
    ``image_url`` containing a ``data:`` URI.

    For all other block types (``input_file``, future types):
    replaces ``file_id`` with ``file_data`` containing a ``data:``
    URI (e.g. ``"data:application/pdf;base64,..."``).  Provider
    adapters parse the URI to extract the media type and payload.

    :param block: A content block dict containing ``file_id``,
        e.g. ``{"type": "input_image", "file_id": "file_abc123"}``.
    :param file_store: Store for file metadata lookups.
    :param artifact_store: Store for binary content fetches.
    :param cache: Optional per-task base64 cache (see
        :func:`resolve_content_references`).
    :param session_id: Optional owning session id used to verify
        session-scoped file ownership, e.g. ``"conv_abc123"``.
    :returns: A new dict with ``file_id`` replaced by inline
        content. All other fields are preserved.
    :raises ValueError: If ``file_id`` is not found in the file
        store — the file was deleted between request validation
        and agent loop execution.
    """
    file_id = block["file_id"]
    owner_session_id = session_id or _session_id_from_block(block)
    file_meta = file_store.get(file_id)
    if file_meta is None or (
        file_meta.session_id is not None and file_meta.session_id != owner_session_id
    ):
        raise ValueError(
            f"Referenced file '{file_id}' no longer exists — "
            f"it may have been deleted after the request was accepted"
        )

    # Use cached base64 if available; otherwise fetch, encode, and cache.
    if cache is not None and file_id in cache:
        encoded = cache[file_id]
    else:
        content_bytes = artifact_store.get(file_id)
        encoded = base64.b64encode(content_bytes).decode("ascii")
        if cache is not None:
            cache[file_id] = encoded

    # Copy all fields except file_id.
    resolved: dict[str, Any] = {k: v for k, v in block.items() if k != "file_id"}

    content_type = _resolve_content_type(file_meta.content_type, file_meta.filename)

    block_type = block.get("type")
    if block_type == "input_image":
        resolved["image_url"] = f"data:{content_type};base64,{encoded}"
    else:
        # input_file and any future type: inline as file_data.
        # Uses a data: URI so providers (OpenAI, etc.) can parse
        # the media type alongside the payload. The Responses API
        # rejects most non-standard text MIMEs here, so coerce
        # to a safe type — see :func:`_safe_file_data_mime`.
        safe_type = _safe_file_data_mime(content_type)
        resolved["file_data"] = f"data:{safe_type};base64,{encoded}"

    return resolved


def _safe_file_data_mime(content_type: str) -> str:
    """
    Coerce *content_type* to one accepted by the OpenAI Responses API
    on ``input_file.file_data``.

    The Responses API restricts ``file_data`` MIMEs to a small allowlist
    (see :data:`_FILE_DATA_PASSTHROUGH_MIMES`). Anything else text-like
    that we'd normally hand back from :func:`_resolve_content_type`
    (``text/yaml``, ``text/x-rust``, ``text/typescript`` and friends, plus
    JSONL-ish ``application/x-*`` variants) collapses to ``text/plain``.
    The base64 payload is unchanged — only the MIME hint shifts — and the
    block's ``filename`` field carries the original extension for the
    model to interpret.

    Non-text types we don't recognise (``image/*``, ``audio/*``,
    third-party ``application/*``) pass through unchanged: we have no
    fixed list there and downgrading them would mislead the provider.

    :param content_type: The precise MIME from
        :func:`_resolve_content_type`, e.g. ``"text/yaml"``.
    :returns: Either *content_type* unchanged (when on the passthrough
        list or non-text) or ``"text/plain"`` (for text-like MIMEs the
        Responses API rejects).
    """
    if content_type in _FILE_DATA_PASSTHROUGH_MIMES:
        return content_type
    if content_type.startswith("text/"):
        return "text/plain"
    if content_type in {
        "application/jsonl",
        "application/x-ndjson",
        "application/x-ipynb+json",
    }:
        return "text/plain"
    return content_type


def _resolve_content_type(
    stored_type: str | None,
    filename: str | None,
) -> str:
    """
    Determine the MIME type for a file, with fallbacks.

    Priority: stored content_type (unless it's the generic
    ``application/octet-stream``) → ``mimetypes.guess_type``
    from filename → ``_EXTRA_MIME_TYPES`` lookup → ``text/plain``
    for text-like extensions → ``application/octet-stream``.

    Some LLM providers (OpenAI) reject ``application/octet-stream``
    for text files, so we try hard to resolve a specific type.

    :param stored_type: The content_type from file metadata, or
        ``None``.
    :param filename: The original filename, e.g. ``"report.md"``.
    MIME parameters are stripped and the stored type is lowercased so inline
    data URIs and upload validation use a canonical bare media type.

    :returns: A MIME type string.
    """
    import mimetypes as _mt
    from pathlib import PurePath

    normalized_stored_type = None
    if stored_type:
        normalized_stored_type = stored_type.split(";", 1)[0].strip().lower() or None

    # Use stored type if it's specific (not the generic fallback).
    if normalized_stored_type and normalized_stored_type != "application/octet-stream":
        return normalized_stored_type

    if filename:
        suffix = PurePath(filename).suffix.lower()
        # Our map takes priority over mimetypes — the stdlib has
        # wrong mappings for some code extensions (e.g. .ts →
        # video/mp2t, .rs → application/rls-services+xml).
        if suffix in _EXTRA_MIME_TYPES:
            return _EXTRA_MIME_TYPES[suffix]
        guessed = _mt.guess_type(filename)[0]
        if guessed and guessed != "application/octet-stream":
            return guessed
        # Text-like extensions default to text/plain rather than
        # octet-stream, which providers are more likely to accept.
        if suffix in {".txt", ".log", ".cfg", ".ini", ".env"}:
            return "text/plain"

    return normalized_stored_type or "application/octet-stream"
