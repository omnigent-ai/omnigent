"""Normalize persisted tool results for safe replay.

Claude-native forks and cold resumes rebuild stored MCP results into Anthropic
content blocks. Image attachments must be normalized so Claude receives one
structured image—not another base64 copy from ``toolUseResult`` metadata.
Otherwise image-heavy histories can exceed the context limit before compaction;
one affected session produced ~2.17M tokens against Claude's 1M-token maximum.

Handles text, single or mixed MCP image blocks, newline-joined results, error
prefixes, and malformed or store-truncated images. Valid images are preserved;
unsafe partial payloads become bounded placeholders. Kept stdlib-only so replay
and compaction paths can import it safely.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass

from omnigent.json_types import JsonObject

_logger = logging.getLogger(__name__)


def _json_object(value: object) -> JsonObject | None:
    """Return a string-keyed object for a decoded JSON mapping.

    A local copy of the same helper in ``claude_native``: duplicated so this
    module stays a leaf with no import back into the harness.
    """
    if not isinstance(value, dict):
        return None
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def image_omitted_placeholder(media_type: str | None) -> str:
    """Return the placeholder text for a stripped inline image.

    :param media_type: Image MIME type when known, e.g. ``"image/png"``.
    :returns: Human/agent-readable placeholder naming how to recover it.
    """
    label = f"{media_type} image" if isinstance(media_type, str) and media_type else "image"
    return (
        f"[{label} omitted from history to save context — "
        "re-run the tool call above (e.g. Read the same path) to view it again]"
    )


#: Prefix ``_format_call_result`` puts on a failed MCP result. It displaces the
#: leading ``[``/``{`` both shape guards look for, so both must strip it.
#: Duplicated, not imported — pinned by
#: ``test_mcp_error_prefix_constant_matches_the_real_formatter``.
_MCP_ERROR_PREFIX = "Error: "


#: An image block's own type discriminator, and the key its payload hangs off.
#: Matching this shape rather than loose ``"image"``/``"base64"`` tokens is what
#: keeps unrelated malformed JSON such as ``{"kind":"image","encoding":"base64"}``
#: from being collapsed, while catching both the Anthropic ``source.data`` form
#: and the bare ``data``/``mimeType`` form MCP persists.
_IMAGE_TYPE_KEY_RE = re.compile(r'"type"\s*:\s*"image"')
_IMAGE_DATA_KEY_RE = re.compile(r'"data"\s*:')


def _holds_clipped_image_payload(body: str) -> bool:
    """Whether *body* looks like an image block carrying a payload.

    :param body: Candidate tool-result text, error prefix already removed.
    :returns: True when an image block's ``data`` key is present.
    """
    return bool(_IMAGE_TYPE_KEY_RE.search(body) and _IMAGE_DATA_KEY_RE.search(body))


def strip_unparseable_image_output(output: str) -> str:
    """Collapse a truncated/corrupt base64 image tool result to a placeholder.

    Intact image outputs (valid JSON) are returned unchanged so the caller can
    rehydrate them into real image blocks for ``--resume``. Only a payload that
    *looks* like an image but no longer parses as JSON — the shape the store
    leaves when it clips at its byte cap — is replaced, so the corrupt base64
    is never sent as prompt text. One :data:`_MCP_ERROR_PREFIX` is stripped
    before that shape check and re-attached as a text block, so an errored
    truncated image is collapsed too without losing the error.

    :param output: The persisted tool-result string.
    :returns: The original string, or a placeholder JSON array when the output
        is an unparseable image payload.
    """
    body = output
    prefix = ""
    if output.startswith(_MCP_ERROR_PREFIX):
        prefix = _MCP_ERROR_PREFIX
        body = output[len(_MCP_ERROR_PREFIX) :]
    stripped = body.lstrip()
    if stripped[:1] not in ("[", "{") or not _holds_clipped_image_payload(body):
        return output
    if "\n" in body:
        # The newline-joined multi-block form is several documents, so failing
        # ``json.loads`` here means nothing; :func:`_blocks_from_newline_joined`
        # collapses just the clipped line and keeps the intact ones.
        return output
    try:
        json.loads(body)
    except (json.JSONDecodeError, ValueError):
        blocks: list[JsonObject] = []
        if prefix:
            blocks.append({"type": "text", "text": prefix.strip()})
        blocks.append({"type": "text", "text": image_omitted_placeholder(None)})
        return json.dumps(blocks, separators=(",", ":"))
    return output


@dataclass(frozen=True)
class RehydratedContent:
    """
    The outcome of normalizing one persisted tool-result string.

    :param blocks: Content blocks, or ``None`` when the output holds no
        recognized shape (the caller keeps the raw string).
    :param dropped_oversized_image: True when an oversized unconvertible
        image payload was replaced with the placeholder — the signal the
        metadata builder needs, since the payload then survives nowhere.
    """

    blocks: list[JsonObject] | None
    dropped_oversized_image: bool = False


def tool_result_content_blocks(output: str) -> RehydratedContent:
    """
    Rehydrate a persisted tool-result string into content blocks.

    Delegates shape matching to :func:`_rehydrate_tool_result_shape`, then
    retries once against a de-prefixed copy: the ``"Error: "`` a failed MCP
    result carries makes an otherwise-parseable lone image object
    unparseable, and the prefix is re-attached as a leading text block so the
    error survives without the payload replaying as text. Only done when an
    image payload is at stake, so errored non-image results keep their exact
    representation.

    :param output: The persisted tool-result string.
    :returns: A :class:`RehydratedContent`; ``blocks`` is ``None`` when
        *output* holds no recognized shape, so the caller keeps the raw string.
    """
    direct = _rehydrate_tool_result_shape(output)
    if direct.blocks is not None or not output.startswith(_MCP_ERROR_PREFIX):
        return direct
    # Exactly one prefix is stripped — a literal "Error: Error: ..." keeps
    # its second copy as text rather than being unwrapped twice.
    nested = _rehydrate_tool_result_shape(output[len(_MCP_ERROR_PREFIX) :])
    if nested.blocks is None:
        return direct
    if not image_payloads_in_blocks(nested.blocks) and not nested.dropped_oversized_image:
        # No payload at stake, so leave the representation alone: an
        # errored non-image result keeps replaying exactly as before.
        return direct
    error_block: JsonObject = {"type": "text", "text": _MCP_ERROR_PREFIX.strip()}
    return RehydratedContent(
        [error_block, *nested.blocks],
        dropped_oversized_image=nested.dropped_oversized_image,
    )


def _rehydrate_tool_result_shape(output: str) -> RehydratedContent:
    """
    Normalize one persisted tool-result string by its literal shape.

    Image tool results persist as a JSON *string*; passing that straight into a
    ``tool_result`` block makes ``claude --resume`` send the base64 as plain
    text, ~250K tokens per screenshot instead of ~1.5K. Three shapes are
    recognized — a JSON block array, a lone MCP ``ImageContent`` object, and
    newline-joined mixed text + image-object lines — and only ``text``/``image``
    blocks are rehydrated, since those are what the API accepts inside a
    ``tool_result``. Anything else stays a raw string.

    Called (not recursed into) by :func:`tool_result_content_blocks`, so
    that retry strips at most one error prefix.

    :param output: The persisted tool-result string.
    :returns: A :class:`RehydratedContent`; ``blocks`` is ``None`` when
        *output* holds no recognized block shape.
    """
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return blocks_from_parsed_list(parsed)
    if isinstance(parsed, dict):
        block = _image_block_from_object(parsed)
        if block is not None:
            return RehydratedContent([block])
        # A lone MCP image object is how a single screenshot persists, so
        # an oversized unconvertible one is exactly the payload that must
        # not replay as text.
        placeholder = _oversized_invalid_image_placeholder(parsed)
        if placeholder is None:
            return RehydratedContent(None)
        return RehydratedContent([placeholder], dropped_oversized_image=True)
    if parsed is not None:
        # A JSON scalar (number, string, bool) — not block content.
        return RehydratedContent(None)
    return _blocks_from_newline_joined(output)


def blocks_from_parsed_list(parsed: list[object]) -> RehydratedContent:
    """
    Normalize a parsed block list, converting MCP image entries.

    :param parsed: Decoded JSON list, e.g.
        ``[{"type": "image", "data": "...", "mimeType": "image/png"}]``.
    :returns: A :class:`RehydratedContent` carrying canonical Claude
        blocks, or ``blocks=None`` when any entry is not a
        ``text``/``image`` block dict (caller keeps the raw string).
    """
    if not parsed:
        return RehydratedContent(None)
    blocks: list[JsonObject] = []
    dropped = False
    for value in parsed:
        block = _json_object(value)
        if block is None:
            return RehydratedContent(None)
        block_type = block.get("type")
        if block_type == "text":
            blocks.append(block)
            continue
        if block_type == "image":
            image_block = _image_block_from_object(block)
            if image_block is None:
                # Unconvertible: keep the raw string (caller's fallback)
                # unless the payload is big enough that replaying it as
                # text is itself the overflow risk.
                placeholder = _oversized_invalid_image_placeholder(block)
                if placeholder is None:
                    return RehydratedContent(None)
                blocks.append(placeholder)
                dropped = True
                continue
            blocks.append(image_block)
            continue
        return RehydratedContent(None)
    return RehydratedContent(blocks, dropped_oversized_image=dropped)


#: Magic-byte prefixes for the image types Claude accepts, keyed by the media
#: type the caller declares. A payload must match its own declared type —
#: Claude is told that type, so a mismatch fails the resume.
_IMAGE_MAGIC_BY_MEDIA_TYPE: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),  # plus b"WEBP" at offset 8, checked below
}

#: Floor below which a payload cannot be a real image of any accepted type.
#: Measured from fixtures, not guessed: the smallest real image here is a
#: 37-byte 1x1 GIF, while the largest non-image shape that still matches a
#: magic prefix is a bare 12-byte ``RIFF``+``WEBP`` header.
_MIN_IMAGE_BYTES = 16

#: Base64 length above which an image-shaped payload that fails validation is
#: collapsed rather than replayed as text — rejecting must not cost more than
#: accepting. 8 KiB: under every real screenshot (the smallest in the reported
#: incident was 32,432 chars), over the snippets docs and tests embed.
_MAX_INVALID_IMAGE_REPLAY_CHARS = 8 * 1024


def canonical_image_payload(data: str, media_type: str) -> str | None:
    """
    Return *data* as canonical base64 when it is plausibly a *media_type* image.

    Producers legitimately emit line-wrapped or unpadded base64, which strict
    decoding rejects, so ASCII whitespace is dropped and ``=`` padding restored
    before validating. The canonical form is returned rather than the original
    because a provider may reject the noncanonical spelling.

    Validation is a declared-type magic-byte match. An invalid image block makes
    Claude reject the resume on every launch, so the bytes must match the type we
    declare to it. Container validation is unnecessary: a store-truncated payload
    never parses as JSON and so never reaches here, which means a magic-valid but
    internally corrupt payload is accepted by design.

    :param data: Candidate base64 payload, possibly wrapped or unpadded.
    :param media_type: Declared MIME type, e.g. ``"image/png"``.
    :returns: Canonical base64, or ``None`` when the payload is unusable.
    """
    magic = _IMAGE_MAGIC_BY_MEDIA_TYPE.get(media_type)
    if magic is None:
        return None
    compact = "".join(data.split())
    if not compact:
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) < _MIN_IMAGE_BYTES or not decoded.startswith(magic):
        return None
    if media_type == "image/webp" and decoded[8:12] != b"WEBP":
        return None
    return padded


def _is_supported_image_payload(data: str, media_type: str) -> bool:
    """
    Whether *data* is plausibly a *media_type* image.

    :param data: Candidate base64 payload.
    :param media_type: Declared MIME type, e.g. ``"image/png"``.
    :returns: True for a payload whose bytes match its declared type.
    """
    return canonical_image_payload(data, media_type) is not None


def _canonicalized_source_block(block: JsonObject, source: JsonObject) -> JsonObject:
    """
    Rewrite an Anthropic-shaped block's payload to canonical base64.

    The provider validates ``source.data`` strictly and rejects the whole request
    on a wrapped payload (``invalid base64 image data: Invalid symbol 13``), so a
    producer's line breaks must not survive into model input. Validation against
    the declared media type is preferred; failing that, whitespace alone is
    normalized when the result still decodes. A payload that cannot be
    normalized safely is passed through untouched, as this shape always has been.

    :param block: The parsed image block.
    :param source: Its ``source`` object.
    :returns: *block*, or a copy carrying the canonical payload.
    """
    data = source.get("data")
    if not isinstance(data, str) or not data:
        return block
    media_type = source.get("media_type")
    canonical = canonical_image_payload(data, media_type) if isinstance(media_type, str) else None
    if canonical is None:
        canonical = _whitespace_normalized_base64(data)
    if canonical is None or canonical == data:
        return block
    return {**block, "source": {**source, "data": canonical}}


def _whitespace_normalized_base64(data: str) -> str | None:
    """
    Strip ASCII whitespace and restore padding, keeping the bytes identical.

    The fallback for a payload whose media type is missing or unsupported: the
    spelling is still unsafe to send, but the bytes must not change.

    :param data: Candidate base64 payload.
    :returns: Canonical base64, or ``None`` when the result would not decode.
    """
    compact = "".join(data.split())
    if not compact or compact == data:
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return padded


def _image_block_from_object(value: object) -> JsonObject | None:
    """
    Return a canonical Claude image block for one parsed image object.

    Accepts the Anthropic wire shape (``source`` present) and the MCP
    ``ImageContent`` serialization (bare ``data`` + ``mimeType``), the latter
    only after payload validation. Either way the emitted block carries the
    canonical base64 spelling, so a wrapped or unpadded original never reaches
    the provider. Anything else yields ``None`` so callers keep the raw string
    rather than emit an API-invalid block.

    :param value: Decoded JSON value, e.g.
        ``{"type": "image", "data": "...", "mimeType": "image/png"}``.
    :returns: ``{"type": "image", "source": {...}}`` or ``None``.
    """
    block = _json_object(value)
    if block is None or block.get("type") != "image":
        return None
    source = block.get("source")
    if isinstance(source, dict):
        return _canonicalized_source_block(block, source)
    data = block.get("data")
    mime = block.get("mimeType")
    if not isinstance(data, str) or not data or not isinstance(mime, str):
        return None
    canonical = canonical_image_payload(data, mime)
    if canonical is None:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": canonical}}


def _oversized_invalid_image_placeholder(value: object) -> JsonObject | None:
    """
    Return a placeholder text block for a large unconvertible image object.

    Applies only where :func:`_image_block_from_object` declined, so the
    object is image-shaped but cannot become an image block. Keeping the payload
    as raw text is fine for a short snippet, not past
    :data:`_MAX_INVALID_IMAGE_REPLAY_CHARS`.

    :param value: Decoded JSON value the image conversion rejected.
    :returns: A text block for an oversized image-shaped object; ``None``
        otherwise, so small payloads keep their existing behavior.
    """
    block = _json_object(value)
    if block is None or block.get("type") != "image":
        return None
    data = block.get("data")
    if not isinstance(data, str) or len(data) <= _MAX_INVALID_IMAGE_REPLAY_CHARS:
        return None
    mime = block.get("mimeType")
    # Diagnostic only — shape, declared type, and size, never the payload.
    _logger.debug(
        "native-claude: collapsed oversized invalid image to placeholder "
        "(keys=%s, media_type=%r, base64_chars=%d, threshold=%d)",
        sorted(block),
        mime,
        len(data),
        _MAX_INVALID_IMAGE_REPLAY_CHARS,
    )
    placeholder = image_omitted_placeholder(mime if isinstance(mime, str) else None)
    return {"type": "text", "text": placeholder}


def _blocks_from_newline_joined(output: str) -> RehydratedContent:
    """
    Recover blocks from newline-joined mixed text + image JSON.

    ``omnigent.tools.mcp._format_call_result`` joins multiple MCP content
    blocks with ``"\\n"``, so a text-plus-screenshot result persists as
    plain text followed by a JSON object line — invalid as one JSON
    document. Splitting per line reproduces the original block stream:
    lines that parse as image objects become image blocks, everything
    else stays verbatim text (``"\\n"``-joined, so the text content is
    byte-identical to the persisted string).

    :param output: The persisted tool-result string.
    :returns: A :class:`RehydratedContent` carrying blocks when at least
        one line holds an image object — converted when its payload
        validates, else collapsed to a placeholder when the payload is too
        large to replay as text; ``blocks=None`` otherwise (caller keeps
        the raw string).
    """
    if "\n" not in output:
        return RehydratedContent(None)
    blocks: list[JsonObject] = []
    text_lines: list[str] = []
    found_image = False
    dropped = False
    for line in output.split("\n"):
        image_block: JsonObject | None = None
        if line.lstrip()[:1] == "{":
            try:
                parsed_line: object | None = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                parsed_line = None
            if parsed_line is not None:
                image_block = _image_block_from_object(parsed_line)
                if image_block is None:
                    # An oversized unconvertible image line becomes a
                    # placeholder rather than replaying its base64 as text.
                    image_block = _oversized_invalid_image_placeholder(parsed_line)
                    if image_block is not None:
                        dropped = True
            elif _holds_clipped_image_payload(line):
                # A store-clipped image line no longer parses; keep the
                # surrounding text and stand the payload down to a placeholder.
                image_block = {"type": "text", "text": image_omitted_placeholder(None)}
                dropped = True
        if image_block is None:
            text_lines.append(line)
            continue
        text = "\n".join(text_lines)
        text_lines = []
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.append(image_block)
        found_image = True
    if not found_image:
        return RehydratedContent(None)
    text = "\n".join(text_lines)
    if text:
        blocks.append({"type": "text", "text": text})
    return RehydratedContent(blocks, dropped_oversized_image=dropped)


def image_payloads_in_blocks(blocks: list[JsonObject]) -> list[str]:
    """
    Return the base64 payloads carried by a block list's image blocks.

    :param blocks: Canonical content blocks.
    :returns: Non-empty ``source.data`` strings, e.g. ``["iVBOR..."]``.
    """
    payloads: list[str] = []
    for block in blocks:
        source = block.get("source")
        if isinstance(source, dict):
            data = source.get("data")
            if isinstance(data, str) and data:
                payloads.append(data)
    return payloads
