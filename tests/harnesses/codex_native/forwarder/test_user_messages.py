"""User messages tests for Codex forwarder."""

from __future__ import annotations

import logging

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


@pytest.mark.parametrize(
    "content,expected",
    [
        ([{"type": "image", "url": "data:image/png;base64,AAAA"}], True),
        ([{"type": "input_file", "file_data": "data:application/pdf;base64,AAAA"}], True),
        ([{"type": "text", "text": "hi"}, {"type": "image", "url": "data:x"}], True),
        ([{"type": "text", "text": "only text"}], False),
        ([], False),
        ("not a list", False),
    ],
)
def test_user_message_has_file_content(content: object, expected: bool) -> None:
    """
    Detect a non-text (image/file) block in a Codex ``userMessage``.

    Drives the gate that decides whether a text-less ``userMessage`` is a
    real image-bearing message that must be persisted. ``True`` for any
    block whose ``type`` is not ``"text"``, else ``False``. A wrong result
    re-opens the image-only regression (text-less image skipped → dropped
    bubble + pending-FIFO bleed) or makes text-only messages post twice.
    """
    assert fwd._user_message_has_file_content({"content": content}) is expected


@pytest.mark.asyncio
async def test_post_user_message_image_only_posts_empty_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    An image-only ``userMessage`` is posted with EMPTY Omnigent content.

    Regression guard for the image-only bleed/ordering bug: the forwarder
    must post the user item (so the server drains the pending-input FIFO
    entry and folds the image in by file_id). The posted content is empty
    — the base64 ``data:`` URL Codex echoes must NOT be written into text.
    A bail here would drop the user bubble and leak the pending entry into
    the next message.
    """
    caplog.set_level(logging.INFO, logger=fwd.__name__)
    client = _RecordingClient()
    item = {
        "type": "userMessage",
        "content": [{"type": "image", "url": "data:image/png;base64,AAAA"}],
    }

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1, "image-only userMessage must still be posted"
    _url, body = client.posts[0]
    item_data = body["data"]["item_data"]
    assert item_data["role"] == "user"
    # Empty content: the image is supplied server-side from the pending
    # entry; echoing Codex's base64 url here would re-introduce the freeze.
    assert item_data["content"] == []
    [observed, finished] = [
        record.attributes
        for record in caplog.records
        if getattr(record, "event_name", "").startswith("native_user_record_")
    ]
    assert observed["has_file"] is True
    assert observed["has_text"] is False
    assert (
        observed["native_source_id"] == finished["native_source_id"] == body["data"]["source_id"]
    )
    assert finished["outcome"] == "forward_accepted"
    assert "data:image" not in repr([observed, finished])


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", ["native-item", "x" * 300])
async def test_post_user_message_text_posts_input_text(
    caplog: pytest.LogCaptureFixture, item_id: str
) -> None:
    """A text ``userMessage`` posts an ``input_text`` block (unchanged path)."""
    caplog.set_level(logging.INFO, logger=fwd.__name__)
    client = _RecordingClient()
    item = {"id": item_id, "type": "userMessage", "content": [{"type": "text", "text": "hello"}]}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["data"]["item_data"]["content"] == [{"type": "input_text", "text": "hello"}]
    [observed, finished] = [
        record.attributes
        for record in caplog.records
        if getattr(record, "event_name", "").startswith("native_user_record_")
    ]
    assert observed["has_text"] is True
    assert observed["has_file"] is False
    assert observed["native_turn_id"] == "t1"
    assert (
        observed["native_source_id"] == finished["native_source_id"] == body["data"]["source_id"]
    )
    assert finished["outcome"] == "forward_accepted"
    assert "hello" not in repr([observed, finished])


@pytest.mark.asyncio
async def test_post_user_message_truly_empty_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """
    A ``userMessage`` with neither text nor a file block is not posted.

    Without this guard the forwarder would emit spurious empty user
    bubbles. A failure (a post recorded) means the empty-skip branch broke.
    """
    caplog.set_level(logging.INFO, logger=fwd.__name__)
    client = _RecordingClient()
    item = {"type": "userMessage", "content": []}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert client.posts == []
    [record] = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "").startswith("native_user_record_")
    ]
    assert record.event_name == "native_user_record_observed"
    assert record.attributes["outcome"] == "empty_record_skipped"
    assert record.attributes["has_text"] is record.attributes["has_file"] is False
