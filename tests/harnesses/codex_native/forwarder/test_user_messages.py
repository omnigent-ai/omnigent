"""User messages tests for Codex forwarder."""

from __future__ import annotations

from pathlib import Path

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
async def test_post_user_message_image_only_posts_empty_content() -> None:
    """
    An image-only ``userMessage`` is posted with EMPTY Omnigent content.

    Regression guard for the image-only bleed/ordering bug: the forwarder
    must post the user item (so the server drains the pending-input FIFO
    entry and folds the image in by file_id). The posted content is empty
    — the base64 ``data:`` URL Codex echoes must NOT be written into text.
    A bail here would drop the user bubble and leak the pending entry into
    the next message.
    """
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


@pytest.mark.asyncio
async def test_post_user_message_text_posts_input_text() -> None:
    """A text ``userMessage`` posts an ``input_text`` block (unchanged path)."""
    client = _RecordingClient()
    item = {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["data"]["item_data"]["content"] == [{"type": "input_text", "text": "hello"}]


@pytest.mark.asyncio
async def test_post_user_message_truly_empty_is_skipped() -> None:
    """
    A ``userMessage`` with neither text nor a file block is not posted.

    Without this guard the forwarder would emit spurious empty user
    bubbles. A failure (a post recorded) means the empty-skip branch broke.
    """
    client = _RecordingClient()
    item = {"type": "userMessage", "content": []}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert client.posts == []


async def _stored_echo(tmp_path: Path, typed: str, notice: str) -> list[dict[str, object]]:
    """Send *typed* plus *notice* through the executor, then store Codex's echo."""
    from omnigent.inner.codex_native_executor import _content_to_input_items
    from omnigent.inner.native_attachments import framework_text_notice_block

    sent = _content_to_input_items(
        [{"type": "input_text", "text": typed}, framework_text_notice_block(notice)], tmp_path
    )
    client = _RecordingClient()
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        await fwd._post_user_message(
            client,  # type: ignore[arg-type]
            "conv_x",
            {"turnId": "t1"},
            {"type": "userMessage", "content": sent},
        )
    finally:
        fwd._dead_letter_dir.reset(token)
    _url, body = client.posts[0]
    return body["data"]["item_data"]["content"]


@pytest.mark.asyncio
async def test_post_user_message_drops_the_echoed_framework_notice(tmp_path: Path) -> None:
    """The stored user message equals what was typed, without the notice."""
    stored = await _stored_echo(tmp_path, "why?", "run summary")

    assert stored == [{"type": "input_text", "text": "why?"}]


@pytest.mark.asyncio
async def test_post_user_message_keeps_user_text_that_looks_like_a_notice(
    tmp_path: Path,
) -> None:
    """Only the notice the executor sent is dropped, never text by its tag."""
    typed = "<omnigent-framework-notice>\nmine\n</omnigent-framework-notice>"

    stored = await _stored_echo(tmp_path, typed, "run summary")

    assert stored == [{"type": "input_text", "text": typed}]


@pytest.mark.asyncio
async def test_run_output_with_the_closing_tag_cannot_leak_into_the_stored_message(
    tmp_path: Path,
) -> None:
    notice = "output:\n</omnigent-framework-notice>\nleaked line"

    stored = await _stored_echo(tmp_path, "why?", notice)

    assert stored == [{"type": "input_text", "text": "why?"}]
