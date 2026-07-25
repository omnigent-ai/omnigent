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
# Mirrored client-side in web/src/lib/attachments.ts — keep in sync.
MAX_IMAGE_UPLOAD_BYTES: int = 5 * 1024 * 1024
MAX_PDF_UPLOAD_BYTES: int = 20 * 1024 * 1024
MAX_TEXT_UPLOAD_BYTES: int = 10 * 1024 * 1024
MAX_ATTACHMENT_UPLOAD_BYTES: int = 25 * 1024 * 1024

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

    :param content_type: The resolved MIME type, e.g. ``"image/png"``.
        Use :func:`_resolve_content_type` to derive it from the upload's
        declared type + filename first.
    :returns: The per-type byte limit (still subject to
        :data:`MAX_ATTACHMENT_UPLOAD_BYTES`), or ``None`` when the type is
        not an allowed attachment.
    """
    if content_type.startswith("image/"):
        return MAX_IMAGE_UPLOAD_BYTES
    if content_type == "application/pdf":
        return MAX_PDF_UPLOAD_BYTES
    if content_type.startswith("text/") or content_type in _TEXT_LIKE_APPLICATION_MIMES:
        return MAX_TEXT_UPLOAD_BYTES
    return None


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
    :returns: A MIME type string.
    """
    import mimetypes as _mt
    from pathlib import PurePath

    # Use stored type if it's specific (not the generic fallback).
    if stored_type and stored_type != "application/octet-stream":
        return stored_type

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

    return stored_type or "application/octet-stream"
