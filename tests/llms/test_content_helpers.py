"""Tests for llms.adapters._content — shared multimodal helpers."""

import pytest

from omnigent.llms.adapters._content import (
    parse_data_uri,
    redact_binary_payloads,
    redact_inline_data_uris,
)


def test_parse_data_uri_png() -> None:
    """Standard image/png data URI parses correctly."""
    result = parse_data_uri("data:image/png;base64,iVBORw0KGgo=")
    assert result is not None
    assert result.media_type == "image/png"
    assert result.data == "iVBORw0KGgo="


def test_parse_data_uri_pdf() -> None:
    """Application/pdf data URI parses correctly."""
    result = parse_data_uri("data:application/pdf;base64,JVBERi0=")
    assert result is not None
    assert result.media_type == "application/pdf"
    assert result.data == "JVBERi0="


def test_parse_data_uri_returns_none_for_https() -> None:
    """External HTTPS URLs return None — not a data URI."""
    assert parse_data_uri("https://example.com/photo.png") is None


def test_parse_data_uri_returns_none_for_http() -> None:
    """External HTTP URLs return None — not a data URI."""
    assert parse_data_uri("http://example.com/photo.png") is None


def test_parse_data_uri_returns_none_for_missing_base64() -> None:
    """
    Data URI without ;base64, separator returns None —
    we only support base64-encoded data URIs.
    """
    assert parse_data_uri("data:text/plain,Hello") is None


@pytest.mark.parametrize(
    ("uri", "expected_type", "expected_data"),
    [
        pytest.param(
            "data:image/jpeg;base64,/9j/4AAQ",
            "image/jpeg",
            "/9j/4AAQ",
            id="jpeg",
        ),
        pytest.param(
            "data:image/webp;base64,UklGR",
            "image/webp",
            "UklGR",
            id="webp",
        ),
        pytest.param(
            "data:audio/wav;base64,UklF",
            "audio/wav",
            "UklF",
            id="audio",
        ),
    ],
)
def test_parse_data_uri_various_types(
    uri: str,
    expected_type: str,
    expected_data: str,
) -> None:
    """Various media types parse correctly."""
    result = parse_data_uri(uri)
    assert result is not None
    assert result.media_type == expected_type
    assert result.data == expected_data


@pytest.mark.parametrize(
    ("value", "expected_marker"),
    [
        pytest.param(
            "data:image/png;charset=binary;base64,QUJD",
            "[image/png:4]",
            id="mime-parameters",
        ),
        pytest.param("data:;base64,QUJD", "[:4]", id="empty-mime"),
        pytest.param("DATA:image/png;BASE64,QUJD", "[image/png:4]", id="uppercase"),
        pytest.param("data:image/png;base64, QUJD", "[image/png:4]", id="space"),
    ],
)
def test_redact_inline_data_uris_accepts_common_variants(
    value: str,
    expected_marker: str,
) -> None:
    """Inline data URI redaction handles safe real-world syntax variants."""
    assert redact_inline_data_uris(value, lambda media_type, size: f"[{media_type}:{size}]") == (
        expected_marker
    )


def test_redact_inline_data_uris_preserves_following_prose() -> None:
    """Redaction stops at the base64 token instead of consuming later prose."""
    value = "preview data:image/png;base64,AAAA then describe the screenshot"

    assert redact_inline_data_uris(value, lambda media_type, size: f"[{media_type}:{size}]") == (
        "preview [image/png:4] then describe the screenshot"
    )


def test_redact_binary_payloads_clears_anthropic_source() -> None:
    """Bare base64 under source.data has no data: prefix for the URI regex."""
    block = {
        "type": "image",
        "file_id": "file_1",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }

    result = redact_binary_payloads(block, lambda media_type, size: f"[{media_type}:{size}]")

    assert result["source"]["data"] == "[image/png:4]"
    assert result["file_id"] == "file_1"
    assert result["source"]["media_type"] == "image/png"


def test_redact_binary_payloads_clears_flat_data_field() -> None:
    """Responses-shaped blocks carry the payload at the top level."""
    block = {"type": "file", "file_id": "file_2", "media_type": "application/pdf", "data": "QUJD"}

    result = redact_binary_payloads(block, lambda media_type, size: f"[{media_type}:{size}]")

    assert result["data"] == "[application/pdf:4]"
    assert result["file_id"] == "file_2"


def test_redact_binary_payloads_recurses_into_nested_content() -> None:
    """A tool-result screenshot sits one level below the message content."""
    value = [
        {
            "type": "tool_result",
            "content": [{"type": "image", "source": {"type": "base64", "data": "QUJD"}}],
        }
    ]

    result = redact_binary_payloads(value, lambda media_type, size: f"[{media_type}:{size}]")

    assert result[0]["content"][0]["source"]["data"] == "[:4]"


def test_redact_binary_payloads_also_handles_inline_data_uris() -> None:
    """Data-URI payloads stay covered so one call handles every harness shape."""
    value = {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}

    result = redact_binary_payloads(value, lambda media_type, size: f"[{media_type}:{size}]")

    assert result["image_url"] == "[image/png:4]"


def test_redact_binary_payloads_leaves_caller_structure_intact() -> None:
    """The redaction returns a copy — callers may be holding shared dicts."""
    source = {"type": "base64", "data": "QUJD"}
    block = {"type": "image", "source": source}

    result = redact_binary_payloads(block, lambda media_type, size: "[cleared]")

    assert source["data"] == "QUJD"
    assert result["source"] is not source


def test_redact_binary_payloads_ignores_non_binary_blocks() -> None:
    """A text block that happens to carry a data key is left alone."""
    value = {"type": "text", "data": "not-a-payload", "text": "hi"}

    assert redact_binary_payloads(value, lambda media_type, size: "[cleared]") == value


def test_redact_binary_payloads_tolerates_malformed_data_field() -> None:
    """A non-string payload must not blow up ingestion with a TypeError."""
    value = {"type": "image", "source": {"type": "base64", "data": {"unexpected": 1}}}

    result = redact_binary_payloads(value, lambda media_type, size: "[cleared]")

    assert result["source"]["data"] == {"unexpected": 1}


def test_redact_binary_payloads_leaves_empty_payload_alone() -> None:
    """An empty data field carries no bytes — don't inflate it into a marker."""
    value = {"type": "image", "source": {"type": "base64", "data": ""}}

    result = redact_binary_payloads(value, lambda media_type, size: "[cleared]")

    assert result["source"]["data"] == ""


@pytest.mark.parametrize("block_type", [{}, [], {"nested": "dict"}, ["a"], 7, None])
def test_redact_binary_payloads_tolerates_non_string_block_type(block_type: object) -> None:
    """An unhashable ``type`` would raise TypeError out of a pydantic validator."""
    value = {"type": block_type, "data": "QUJD"}

    result = redact_binary_payloads(value, lambda media_type, size: "[cleared]")

    assert result == value


def test_redact_binary_payloads_redacts_before_recursing() -> None:
    """The payload is dropped before the URI regex gets a chance to scan it."""
    calls: list[int] = []

    def marker(media_type: str, size: int) -> str:
        calls.append(size)
        return "[cleared]"

    payload = "data:image/png;base64," + "A" * 4096
    value = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": payload},
    }

    result = redact_binary_payloads(value, marker)

    assert result["source"]["data"] == "[cleared]"
    # One call, sized from the whole payload. Recursing first would scan it
    # with the URI regex and then re-measure the marker it left behind.
    assert calls == [len(payload)]


def test_redact_binary_payloads_still_recurses_into_source_siblings() -> None:
    """A data: URI elsewhere in ``source`` survives the reordered redaction."""
    value = {"type": "image", "source": {"type": "url", "url": "data:image/png;base64,QUJD"}}

    result = redact_binary_payloads(value, lambda media_type, size: f"[{media_type}:{size}]")

    assert result["source"]["url"] == "[image/png:4]"
