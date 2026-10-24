"""User messages tests for Codex forwarder."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.inner import native_attachments
from omnigent.inner.native_attachments import materialize_text
from omnigent.session_event_batch import MAX_SESSION_EVENT_REQUEST_BYTES
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


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge_known", [True, False], ids=["bridge", "no-bridge"])
async def test_post_user_message_mirrors_a_spilled_paste_as_sent(
    tmp_path: Path, bridge_known: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server receives the paste itself, not the cache reference Codex was given."""
    paste = "line of a long log\n" * 50
    reference = f"[Attached file: {materialize_text(paste, tmp_path)}]"
    item = {
        "type": "userMessage",
        "content": [{"type": "text", "text": "what failed?"}, {"type": "text", "text": reference}],
    }
    client = _RecordingClient()
    loop = asyncio.get_running_loop()
    read_started = asyncio.Event()
    read_release = threading.Event()
    loop_progressed: list[bool] = []
    read_cache = native_attachments._read_cached_file

    def slow_read(cache: Path, name: str) -> bytes | None:
        loop.call_soon_threadsafe(read_started.set)
        loop_progressed.append(read_release.wait(timeout=1.0))
        return read_cache(cache, name)

    monkeypatch.setattr(native_attachments, "_read_cached_file", slow_read)
    task = asyncio.create_task(
        fwd._post_user_message(
            client, "conv_x", {"turnId": "t1"}, item, bridge_dir=tmp_path if bridge_known else None
        )
    )
    try:
        if bridge_known:
            await asyncio.wait_for(read_started.wait(), timeout=2.0)
        read_release.set()
        await task
    finally:
        read_release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert loop_progressed == ([True] if bridge_known else [])
    [(_url, body)] = client.posts
    sent = paste if bridge_known else reference
    assert body["data"]["item_data"]["content"] == [
        {"type": "input_text", "text": f"what failed?\n\n{sent}"}
    ]


@pytest.mark.asyncio
async def test_post_user_message_restores_a_paste_whose_whole_event_fits(tmp_path: Path) -> None:
    """Sizing uses the real event, so a paste just under the limit is mirrored in full."""
    paste = "x" * 10_450_000
    reference = f"[Attached file: {materialize_text(paste, tmp_path)}]"
    item = {"type": "userMessage", "content": [{"type": "text", "text": reference}]}
    client = _RecordingClient()

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item, bridge_dir=tmp_path)

    [(_url, body)] = client.posts
    assert body["data"]["item_data"]["content"] == [{"type": "input_text", "text": paste}]


async def test_post_user_message_keeps_the_reference_only_past_the_event_limit(
    tmp_path: Path,
) -> None:
    """The executor refuses such a paste first; this only guards a bypassed check."""
    paste = "x" * MAX_SESSION_EVENT_REQUEST_BYTES
    reference = f"[Attached file: {materialize_text(paste, tmp_path)}]"
    item = {"type": "userMessage", "content": [{"type": "text", "text": reference}]}
    client = _RecordingClient()

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item, bridge_dir=tmp_path)

    [(_url, body)] = client.posts
    assert body["data"]["item_data"]["content"] == [{"type": "input_text", "text": reference}]
