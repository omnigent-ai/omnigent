"""User messages tests for Codex forwarder."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


class _ScriptedEventClient(httpx.AsyncClient):
    """Return scripted event responses while recording accepted POST attempts."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.posts: list[dict[str, Any]] = []
        self.accepted_posts: list[dict[str, Any]] = []

    async def post(  # type: ignore[override]
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float | None = None,
    ) -> httpx.Response:
        del timeout
        self.posts.append(json)
        status = self.statuses.pop(0)
        if status < 400:
            self.accepted_posts.append(json)
        return httpx.Response(status, request=httpx.Request("POST", url), json={})


class _ResumeClient:
    """Small Codex app-server double for recovered user-message tests."""

    def __init__(self, *, stable_user_id: bool = True) -> None:
        self.stable_user_id = stable_user_id

    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        assert method == "thread/resume"
        assert params == {"threadId": "thread_1"}
        user_item: dict[str, Any] = {
            "type": "userMessage",
            "content": [{"type": "text", "text": "hello"}],
        }
        if self.stable_user_id:
            user_item["id"] = "user_1"
        return {
            "result": {
                "thread": {
                    "turns": [
                        {
                            "id": "turn_1",
                            "items": [user_item],
                        }
                    ]
                }
            }
        }


def _user_params() -> dict[str, object]:
    """Build one stable-id completed user item."""
    return {
        "threadId": "thread_1",
        "turnId": "turn_1",
        "item": {
            "type": "userMessage",
            "id": "user_1",
            "content": [{"type": "text", "text": "hello"}],
        },
    }


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
async def test_rejected_user_claim_is_retried_on_replay() -> None:
    """A rejected completed user item must not poison the replay dedupe gate."""
    client = _ScriptedEventClient([422, 202])
    state = fwd._CodexForwarderState()

    await fwd._handle_completed_item_inner(client, "conv_x", _user_params(), forwarder_state=state)
    await fwd._handle_completed_item_inner(client, "conv_x", _user_params(), forwarder_state=state)

    assert len(client.posts) == 2
    assert state.synced_item_keys == {"thread_1:turn_1:user_1"}
    assert state.pending_item_claims == set()


@pytest.mark.asyncio
async def test_recovered_user_claim_is_retried_by_resume_backfill() -> None:
    """A rejected recovery does not hide valid assistant output or poison replay."""
    client = _ScriptedEventClient([422, 202, 202])
    state = fwd._CodexForwarderState(
        codex_client=_ResumeClient(),  # type: ignore[arg-type]
    )
    assistant_params: dict[str, Any] = {
        "threadId": "thread_1",
        "turnId": "turn_1",
        "item": {"type": "agentMessage", "id": "assistant_1", "text": "reply"},
    }

    await fwd._handle_completed_item_inner(
        client, "conv_x", assistant_params, forwarder_state=state
    )
    # A permanent rejection must not hide the valid assistant output forever.
    # The later resume replay still retries the user item because its stable
    # claim was released.
    await fwd._handle_completed_item_inner(client, "conv_x", _user_params(), forwarder_state=state)
    await fwd._handle_completed_item_inner(
        client, "conv_x", assistant_params, forwarder_state=state
    )

    roles = [post["data"]["item_data"]["role"] for post in client.accepted_posts]
    assert roles == ["assistant", "user"]
    assert state.has_posted_user_message("turn_1")
    assert state.pending_item_claims == set()


@pytest.mark.asyncio
async def test_anonymous_assistant_recovery_uses_next_positional_key() -> None:
    """An anonymous assistant must not collide with its recovered user item."""
    client = _ScriptedEventClient([202, 202])
    state = fwd._CodexForwarderState(
        codex_client=_ResumeClient(stable_user_id=False),  # type: ignore[arg-type]
    )
    assistant_params: dict[str, Any] = {
        "threadId": "thread_1",
        "turnId": "turn_1",
        "item": {"type": "agentMessage", "text": "reply"},
    }

    await fwd._handle_completed_item_inner(
        client, "conv_x", assistant_params, forwarder_state=state
    )

    assert [post["data"]["item_data"]["role"] for post in client.accepted_posts] == [
        "user",
        "assistant",
    ]
    assert [post["data"]["source_id"] for post in client.accepted_posts] == [
        "thread_1:turn_1:anon-1",
        "thread_1:turn_1:anon-0",
    ]
    assert state.pending_item_claims == set()
    assert state.peek_anon_item_key("thread_1", "turn_1") == "thread_1:turn_1:anon-2"


@pytest.mark.asyncio
async def test_cancelled_user_post_releases_claim_for_retry() -> None:
    """Cancellation while a user POST has no response must not suppress replay."""
    entered = asyncio.Event()
    pending = asyncio.get_running_loop().create_future()

    class _BlockingClient(_ScriptedEventClient):
        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            timeout: float | None = None,
        ) -> httpx.Response:
            self.posts.append(json)
            if len(self.posts) == 1:
                entered.set()
                await pending
            return httpx.Response(202, request=httpx.Request("POST", url), json={})

    client = _BlockingClient([])
    state = fwd._CodexForwarderState()
    task = asyncio.create_task(
        fwd._handle_completed_item_inner(client, "conv_x", _user_params(), forwarder_state=state)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.pending_item_claims == set()
    assert state.synced_item_keys == set()
    await fwd._handle_completed_item_inner(client, "conv_x", _user_params(), forwarder_state=state)
    assert len(client.posts) == 2
    assert state.synced_item_keys == {"thread_1:turn_1:user_1"}


@pytest.mark.asyncio
async def test_successful_replay_dedup_and_anonymous_counter_commit() -> None:
    """Accepted message claims dedupe, while anonymous keys advance on success."""
    client = _ScriptedEventClient([202, 422, 202, 202])
    state = fwd._CodexForwarderState()
    stable: dict[str, Any] = _user_params()

    await fwd._handle_completed_item_inner(client, "conv_x", stable, forwarder_state=state)
    await fwd._handle_completed_item_inner(client, "conv_x", stable, forwarder_state=state)
    anonymous: dict[str, Any] = {
        "threadId": "thread_1",
        "turnId": "turn_1",
        "item": {"type": "agentMessage", "text": "anonymous"},
    }
    await fwd._handle_completed_item_inner(client, "conv_x", anonymous, forwarder_state=state)
    await fwd._handle_completed_item_inner(client, "conv_x", anonymous, forwarder_state=state)

    sources = [post["data"]["source_id"] for post in client.posts]
    assert sources == [
        "thread_1:turn_1:user_1",
        "thread_1:turn_1:anon-0",
        "thread_1:turn_1:anon-1",
    ]
    assert state.peek_anon_item_key("thread_1", "turn_1") == "thread_1:turn_1:anon-2"
