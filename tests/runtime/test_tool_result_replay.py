"""Tests for persisted tool-result normalization and image-payload plausibility."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from omnigent.runtime import tool_result_replay as trc
from tests._image_fixtures import (
    _TINY_CMYK_JPEG_BASE64,
    _TINY_GIF_BASE64,
    _TINY_JPEG_BASE64,
    _TINY_PNG_BASE64,
    _TINY_PROGRESSIVE_GRAY_JPEG_BASE64,
    _TINY_PROGRESSIVE_JPEG_BASE64,
    _TINY_WEBP_BASE64,
    mcp_call_output,
)


def test_collapsed_oversized_image_logs_shape_without_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Collapsing a payload leaves a breadcrumb, but never the payload.

    Dropping content silently would make a shrunken transcript
    unexplainable after the fact, so the collapse logs at debug level
    with the object's shape, declared media type, and size. The payload
    itself must never reach the logs.

    Patches ``_logger.debug`` directly rather than using caplog, matching
    the other log assertions in this module: caplog depends on handler /
    propagation state that other tests' logging setup can disturb.
    """
    oversized = base64.b64encode(b"not an image payload " * 2_000).decode()
    captured: list[str] = []
    monkeypatch.setattr(
        trc._logger,
        "debug",
        lambda msg, *args: captured.append(msg % args),
    )
    block = trc._oversized_invalid_image_placeholder(
        {"type": "image", "data": oversized, "mimeType": "image/jpeg"}
    )
    assert block is not None
    assert len(captured) == 1
    message = captured[0]
    assert "collapsed oversized invalid image" in message
    assert "'image/jpeg'" in message
    assert str(len(oversized)) in message
    assert "'data'" in message and "'mimeType'" in message
    assert oversized[:64] not in message
    # A sub-threshold payload is not collapsed, so it logs nothing.
    captured.clear()
    small = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 64).decode()
    assert (
        trc._oversized_invalid_image_placeholder(
            {"type": "image", "data": small, "mimeType": "image/jpeg"}
        )
        is None
    )
    assert captured == []


def test_image_payload_gate_accepts_and_rejects() -> None:
    """
    What the payload gate does and does not check.

    It enforces four things — a known declared media type, strict base64, the
    byte floor, and the declared type's magic bytes (with WebP's ``WEBP`` at
    offset 8) — and nothing about container internals. So a truncated or
    CRC-corrupted payload that keeps its signature is *accepted*; the cases
    below pin both halves of that contract.
    """
    gate = trc._is_supported_image_payload
    for mime_type, payload in (
        ("image/png", _TINY_PNG_BASE64),
        ("image/jpeg", _TINY_JPEG_BASE64),
        ("image/gif", _TINY_GIF_BASE64),
        ("image/webp", _TINY_WEBP_BASE64),
        ("image/jpeg", _TINY_PROGRESSIVE_JPEG_BASE64),
        ("image/jpeg", _TINY_PROGRESSIVE_GRAY_JPEG_BASE64),
        ("image/jpeg", _TINY_CMYK_JPEG_BASE64),
    ):
        assert gate(payload, mime_type), mime_type
        # Every real fixture clears the floor — that is how it was chosen.
        assert len(base64.b64decode(payload)) >= trc._MIN_IMAGE_BYTES
    # Rejected: unknown type, bad base64, non-image bytes, MIME/signature
    # mismatch either way round.
    assert not gate(base64.b64encode(b"<svg/>" * 8).decode(), "image/svg+xml")
    assert not gate("!!! not base64 !!!", "image/png")
    assert not gate(base64.b64encode(b"plain text " * 8).decode(), "image/png")
    assert not gate(_TINY_PNG_BASE64, "image/jpeg")
    assert not gate(_TINY_JPEG_BASE64, "image/png")
    # Rejected: below the byte floor, even with the right signature.
    for magic, mime_type in (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF89a", "image/gif"),
    ):
        assert len(magic) < trc._MIN_IMAGE_BYTES
        assert not gate(base64.b64encode(magic).decode(), mime_type)
    # Rejected: a RIFF container that is not WebP, and a bare RIFF/WEBP header.
    assert not gate(
        base64.b64encode(b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 40).decode(), "image/webp"
    )
    assert not gate(base64.b64encode(b"RIFF\x00\x00\x00\x00WEBP").decode(), "image/webp")
    # Accepted by design: signature intact, container corrupt or cut short.
    corrupted_png = bytearray(base64.b64decode(_TINY_PNG_BASE64))
    corrupted_png[20] ^= 0xFF
    assert gate(base64.b64encode(bytes(corrupted_png)).decode(), "image/png")
    assert gate(base64.b64encode(base64.b64decode(_TINY_JPEG_BASE64)[:100]).decode(), "image/jpeg")
    assert gate(
        base64.b64encode(base64.b64decode(_TINY_JPEG_BASE64) + b"\x00\x00").decode(),
        "image/jpeg",
    )


def test_tool_result_content_blocks_rehydrates_only_block_arrays() -> None:
    """Only a non-empty list of text/image block dicts rehydrates; else ``None``."""

    def fn(output: str) -> list[dict[str, Any]] | None:
        """Normalize *output*, asserting nothing was dropped."""
        rehydrated = trc.tool_result_content_blocks(output)
        assert rehydrated.dropped_oversized_image is False
        return rehydrated.blocks

    # An image content-block array rehydrates to the parsed list.
    assert fn('[{"type":"image","source":{"type":"base64","data":"AAA"}}]') == [
        {"type": "image", "source": {"type": "base64", "data": "AAA"}}
    ]
    # A text block array rehydrates too.
    assert fn('[{"type":"text","text":"hi"}]') == [{"type": "text", "text": "hi"}]
    # Plain text is not JSON → keep the raw string.
    assert fn("file written") is None
    # A JSON string / number / object is not a block array → keep raw.
    assert fn('"just a string"') is None
    assert fn("42") is None
    assert fn('{"type":"image"}') is None
    # An empty array carries nothing to rehydrate.
    assert fn("[]") is None
    # A list whose entries are not typed block dicts is not a block array.
    assert fn('["a","b"]') is None
    assert fn('[{"no_type":1}]') is None
    # A typed block the API does not accept in a tool_result stays a raw
    # string, so resume keeps sending exactly what it sent before.
    assert fn('[{"type":"file","path":"/x"}]') is None


def test_strip_unparseable_image_output_negatives_are_byte_for_byte() -> None:
    """
    Direct negative: only a truncated *image* payload is ever collapsed.

    The prefix is not a licence to restructure errored results in general.
    Anything that is not an unparseable image payload — plain text, an
    errored message, non-image JSON, a text block array, and an intact
    image (which must stay rehydratable) — comes back identical.
    """
    strip = trc.strip_unparseable_image_output
    b64 = _TINY_PNG_BASE64
    intact_source = json.dumps(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            }
        ],
        separators=(",", ":"),
    )
    for output in (
        "file written",
        "Error: tool exploded",
        'Error: {"foo":1}',
        'Error: [{"type":"text","text":"nope"}]',
        intact_source,
        f"Error: {intact_source}",
        json.dumps({"type": "image", "data": b64, "mimeType": "image/png"}),
    ):
        assert strip(output) == output, output[:60]
    # Positive control, with and without the prefix.
    truncated = '[{"type":"image","source":{"type":"base64","data":"' + b64
    assert strip(truncated) != truncated
    assert b64 not in strip(truncated)
    assert b64 not in strip(f"Error: {truncated}")


def test_mcp_error_prefix_constant_matches_the_real_formatter() -> None:
    """
    The prefix constant tracks the formatter that actually emits it.

    ``claude_native`` hardcodes the prefix rather than importing it —
    ``omnigent.tools.mcp`` builds it inline as an f-string, and importing
    that module from the transcript builder would couple two layers for a
    seven-character string. This asserts the duplication stays honest, so
    changing the formatter's wording fails here instead of silently
    reopening the base64 leak.
    """
    from mcp.types import TextContent

    errored = mcp_call_output(TextContent(type="text", text="boom"), is_error=True)
    plain = mcp_call_output(TextContent(type="text", text="boom"))
    assert errored == f"{trc._MCP_ERROR_PREFIX}{plain}"
    assert not plain.startswith(trc._MCP_ERROR_PREFIX)


def test_truncated_image_detection_keys_on_shape_not_tokens() -> None:
    """The collapse guard matches an image block's shape, not loose tokens.

    Both persisted spellings must collapse when clipped — Anthropic
    ``source.data`` and the bare MCP ``data``/``mimeType`` — while malformed
    JSON that merely mentions the words is left alone.
    """
    strip = trc.strip_unparseable_image_output
    payload = "iVBORw0KGgo" + "A" * 4_000
    clipped = {
        "anthropic source": '[{"type":"image","source":{"type":"base64","data":"' + payload,
        "mcp bare data": '{"type":"image","data":"' + payload,
        "mcp with mimeType first": '{"mimeType":"image/png","type":"image","data":"' + payload,
        "errored mcp": 'Error: {"type":"image","data":"' + payload,
        "spaced keys": '{"type" : "image", "data" : "' + payload,
    }
    for label, output in clipped.items():
        collapsed = strip(output)
        assert collapsed != output, label
        assert payload[:64] not in collapsed, label
        assert "omitted from history" in collapsed, label
    untouched = {
        # Mentions both words but is not an image block.
        "kind/encoding decoy": '{"kind":"image","encoding":"base64","data":"' + payload,
        "truncated text block": '{"type":"text","text":"hello wor',
        # Clipped before the payload key: nothing to stand down.
        "no data key yet": '{"type":"image","mimeType":"image/pn',
        "plain prose": "file written",
    }
    for label, output in untouched.items():
        assert strip(output) == output, label


@pytest.mark.parametrize(
    "spell",
    ["canonical", "line-wrapped", "unpadded", "spaced"],
)
def test_normal_base64_variants_are_accepted_and_canonicalized(spell: str) -> None:
    """Wrapped or unpadded base64 is normal producer output, not corruption.

    Strict decoding rejects both, which used to turn a perfectly good screenshot
    into an omission placeholder. The emitted block carries the canonical
    spelling so a provider cannot reject the original's formatting.
    """
    canonical = base64.b64encode(base64.b64decode(_TINY_PNG_BASE64)).decode()
    payload = {
        "canonical": canonical,
        "line-wrapped": "\n".join(canonical[i : i + 76] for i in range(0, len(canonical), 76)),
        "unpadded": canonical.rstrip("="),
        "spaced": " ".join(canonical[i : i + 40] for i in range(0, len(canonical), 40)),
    }[spell]

    assert trc.canonical_image_payload(payload, "image/png") == canonical
    block = trc._image_block_from_object(
        {"type": "image", "data": payload, "mimeType": "image/png"}
    )
    assert block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": canonical},
    }


def test_unusable_payloads_stay_omitted() -> None:
    """Malformed, mismatched, unsupported and clipped payloads are still refused."""
    truncated = _TINY_PNG_BASE64[: len(_TINY_PNG_BASE64) // 2]
    for payload, mime in (
        ("!!! not base64 !!!", "image/png"),
        (base64.b64encode(b"plain text here" * 4).decode(), "image/png"),
        (_TINY_PNG_BASE64, "image/svg+xml"),
        (base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(), "image/png"),
        (_TINY_PNG_BASE64, "image/jpeg"),
    ):
        assert trc.canonical_image_payload(payload, mime) is None
    # A clipped payload may still decode; it must not pass as a PNG.
    assert trc.canonical_image_payload(truncated, "image/png") is None or (
        base64.b64decode(trc.canonical_image_payload(truncated, "image/png") or "").startswith(
            b"\x89PNG\r\n\x1a\n"
        )
    )
