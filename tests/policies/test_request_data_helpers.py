"""Tests for the request-phase data helpers in ``omnigent.policies.schema``.

``request_user_text`` and ``request_attachments`` normalize the REQUEST-phase
``event["data"]`` — a ``{"user_content", "attachments"}`` dict from the server
input gate, or a bare string / content-block list from native / opencode hooks —
so request-phase policies read the typed message and attachments uniformly.
"""

from __future__ import annotations

from omnigent.policies.schema import request_attachments, request_user_text


def test_request_user_text_from_dict() -> None:
    """The structured dict shape returns ``user_content``."""
    data = {"user_content": "hello", "attachments": [{"text": "ignored"}]}
    assert request_user_text(data) == "hello"


def test_request_user_text_from_string() -> None:
    """Native / opencode hooks pass the prompt text directly as a string."""
    assert request_user_text("hello there") == "hello there"


def test_request_user_text_from_content_block_list() -> None:
    """A legacy multimodal content-block list joins its text blocks."""
    blocks = [
        {"type": "input_text", "text": "a"},
        {"type": "input_image", "file_id": "img"},
        {"type": "input_text", "text": "b"},
    ]
    assert request_user_text(blocks) == "a\nb"


def test_request_user_text_missing_or_non_string() -> None:
    """Absent / non-string content yields an empty string, never raises."""
    assert request_user_text(None) == ""
    assert request_user_text({"attachments": []}) == ""
    assert request_user_text({"user_content": 123}) == ""


def test_request_attachments_from_dict() -> None:
    """Attachments come back as-is from the structured dict."""
    atts = [{"filename": "a.csv", "content_type": "text/csv", "text": "x"}]
    assert request_attachments({"user_content": "", "attachments": atts}) == atts


def test_request_attachments_absent() -> None:
    """No attachments field (or non-dict data) yields an empty list."""
    assert request_attachments("plain string") == []
    assert request_attachments({"user_content": "hi"}) == []
    assert request_attachments(None) == []
