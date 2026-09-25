"""Unit tests for Codex ``/side`` follow-up routing.

Covers the two pure-ish seams that carry a user's follow-up turn into the side
chat's Codex thread: the server redirect (`_forward_codex_side_chat_turn`) and
the runner's message-text extraction (`_side_chat_text_from_content`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

_JsonObject = dict[str, Any]


def test_side_chat_text_from_content_joins_text_blocks() -> None:
    from omnigent.runner.app import _side_chat_text_from_content

    assert _side_chat_text_from_content([{"type": "input_text", "text": "and why?"}]) == "and why?"
    assert (
        _side_chat_text_from_content(
            [
                {"type": "text", "text": "a"},
                {"type": "image", "url": "x"},  # non-text block ignored
                {"type": "input_text", "text": "b"},
            ]
        )
        == "a\nb"
    )
    assert _side_chat_text_from_content("nope") == ""  # not a list
    assert _side_chat_text_from_content([]) == ""


class _FakeResp:
    def raise_for_status(self) -> None:
        return None


class _FakeRunnerClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, _JsonObject]] = []

    async def post(self, url: str, *, json: _JsonObject) -> _FakeResp:
        self.posts.append((url, json))
        return _FakeResp()


@pytest.mark.asyncio
async def test_forward_side_chat_turn_redirects_to_parent_with_thread_id() -> None:
    from omnigent.server.routes._sessions import orchestration as orch
    from omnigent.server.routes._sessions.common import _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY

    conv = SimpleNamespace(
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        labels={_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"},
    )
    body = SimpleNamespace(data={"content": [{"type": "input_text", "text": "and why?"}]})
    client = _FakeRunnerClient()

    result = await orch._forward_codex_side_chat_turn(conv, body, client)

    assert result is not None and result.item_id is None  # no AP-side persist
    assert len(client.posts) == 1
    url, payload = client.posts[0]
    assert "conv_parent" in url and url.endswith("/events")  # forwarded to the PARENT
    assert payload["codex_side_thread_id"] == "thread_side"  # tagged with the child thread
    assert payload["content"] == [{"type": "input_text", "text": "and why?"}]


@pytest.mark.asyncio
async def test_forward_side_chat_turn_falls_through_without_thread_or_parent() -> None:
    from omnigent.server.routes._sessions import orchestration as orch
    from omnigent.server.routes._sessions.common import _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY

    body = SimpleNamespace(data={"content": []})
    client = _FakeRunnerClient()

    conv_no_label = SimpleNamespace(
        parent_conversation_id="conv_parent", kind="sub_agent", labels={}
    )
    assert await orch._forward_codex_side_chat_turn(conv_no_label, body, client) is None

    conv_no_parent = SimpleNamespace(
        parent_conversation_id=None,
        kind="sub_agent",
        labels={_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"},
    )
    assert await orch._forward_codex_side_chat_turn(conv_no_parent, body, client) is None

    assert client.posts == []  # nothing forwarded when it should fall through
