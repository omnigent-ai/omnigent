"""User messages tests for Codex forwarder."""

from __future__ import annotations

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


@pytest.mark.parametrize(
    ("echoing_replay", "copies", "expected_duplicates"),
    [
        (True, ["resumed", "live"], [False, True]),
        (True, ["live", "resumed"], [False, True]),
        (True, ["resumed", "resumed", "live", "live"], [False, False, True, True]),
        (True, ["resumed", "live", "live"], [False, True, False]),
        (False, ["resumed", "live"], [False, False]),
        (False, ["live", "resumed", "resumed"], [False, True, False]),
    ],
    ids=[
        "replay_then_echo",
        "echo_then_replay",
        "repeated_input",
        "identical_steer_after_echo",
        "late_join_then_identical_steer",
        "live_then_late_join_replay",
    ],
)
def test_pair_user_message_copy_matches_each_resumed_input_once(
    echoing_replay: bool, copies: list[str], expected_duplicates: list[bool]
) -> None:
    """Each resumed copy pairs with at most one live copy of the same input."""
    state = fwd._CodexForwarderState(parent_session_id="conv_123")
    if echoing_replay:
        state.resume_echo_thread_ids.add("thread_123")
    key = ("thread_123", "turn_123", '[{"text": "Run the tests again.", "type": "text"}]')

    duplicates = [state.pair_user_message_copy(key, resumed=copy == "resumed") for copy in copies]

    assert duplicates == expected_duplicates


def test_user_message_copy_balance_keeps_unmatched_copies_until_turn_ends() -> None:
    """Paired copies and a finished turn's copies are not retained."""
    state = fwd._CodexForwarderState(parent_session_id="conv_123")
    state.resume_echo_thread_ids.add("thread_123")
    paired = ("thread_123", "turn_123", "digest_paired")
    unmatched = ("thread_123", "turn_123", "digest_unmatched")
    other_turn = ("thread_123", "turn_456", "digest_unmatched")

    state.pair_user_message_copy(paired, resumed=True)
    state.pair_user_message_copy(paired, resumed=False)
    state.pair_user_message_copy(unmatched, resumed=False)
    state.pair_user_message_copy(other_turn, resumed=False)
    assert state.user_message_copy_balance == {unmatched: 1, other_turn: 1}

    state.forget_user_message_copies("turn_123")
    assert state.user_message_copy_balance == {other_turn: 1}
