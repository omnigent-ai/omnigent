"""Tests for cross-session (peer) messaging on the runner.

Covers ``_execute_peer_message_tool`` and its inbound-depth reader
(``_read_inbound_chain_depth``): the delivered payload shape, the
``source_session_id`` / ``chain_depth`` markers, the chain-depth loop guard,
and the error mappings (unknown target, feature disabled, bad args). The
server side is faked with an ``httpx.MockTransport`` so the tool is exercised
in isolation from a live server.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _MAX_PEER_CHAIN_DEPTH,
    _execute_peer_message_tool,
)

_SELF = "conv_self"
_TARGET = "conv_target"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build an AsyncClient whose requests are served by *handler*."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    )


def _items_response(items: list[dict[str, object]]) -> httpx.Response:
    """A ``GET .../items`` response (newest-first ``data`` list)."""
    return httpx.Response(200, json={"data": items})


def _user_message_item(*, chain_depth: int | None = None) -> dict[str, object]:
    """An API-shape inbound user message item, optionally peer-stamped."""
    item: dict[str, object] = {
        "id": "item_x",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    if chain_depth is not None:
        item["chain_depth"] = chain_depth
    return item


def _make_handler(
    *,
    items: list[dict[str, object]],
    post_response: httpx.Response,
    recorder: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """
    Route the two calls the tool makes: the self-history read and the
    target POST. Also serves the snapshot GET used for turn-actor lookup.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/sessions/{_SELF}/items"):
            return _items_response(items)
        if request.method == "GET" and path.endswith(f"/sessions/{_SELF}"):
            # Turn-actor snapshot lookup: no attribution label.
            return httpx.Response(200, json={"labels": {}})
        if request.method == "POST" and path.endswith(f"/sessions/{_TARGET}/events"):
            return post_response
        return httpx.Response(500, json={"error": f"unexpected {request.method} {path}"})

    return handler


async def _run(
    *,
    items: list[dict[str, object]],
    post_response: httpx.Response,
    recorder: list[httpx.Request] | None = None,
    args: dict[str, object] | None = None,
) -> dict[str, object]:
    handler = _make_handler(items=items, post_response=post_response, recorder=recorder)
    async with _client(handler) as client:
        out = await _execute_peer_message_tool(
            args if args is not None else {"session_id": _TARGET, "message": "hello"},
            server_client=client,
            conversation_id=_SELF,
        )
    return json.loads(out)


@pytest.mark.asyncio
async def test_delivers_and_stamps_source_and_depth_from_human_turn() -> None:
    """A human-triggered send stamps source_session_id and chain_depth=1."""
    recorder: list[httpx.Request] = []
    result = await _run(
        items=[_user_message_item()],  # human message: no chain_depth
        post_response=httpx.Response(202, json={"queued": True, "item_id": "item_new"}),
        recorder=recorder,
    )
    assert result["delivered"] is True
    assert result["chain_depth"] == 1
    assert result["item_id"] == "item_new"

    post = next(r for r in recorder if r.method == "POST")
    body = json.loads(post.content)
    assert body["type"] == "message"
    assert body["data"]["source_session_id"] == _SELF
    assert body["data"]["chain_depth"] == 1
    assert body["data"]["content"] == [{"type": "input_text", "text": "hello"}]


@pytest.mark.asyncio
async def test_inherits_and_increments_inbound_chain_depth() -> None:
    """A peer-triggered turn sends at inbound_depth + 1."""
    result = await _run(
        items=[_user_message_item(chain_depth=5)],
        post_response=httpx.Response(202, json={"queued": True, "item_id": "i"}),
    )
    assert result["delivered"] is True
    assert result["chain_depth"] == 6


@pytest.mark.asyncio
async def test_loop_guard_pauses_at_threshold_without_posting() -> None:
    """At the depth limit the tool declines to send (soft pause)."""
    recorder: list[httpx.Request] = []
    result = await _run(
        items=[_user_message_item(chain_depth=_MAX_PEER_CHAIN_DEPTH)],
        post_response=httpx.Response(202, json={"item_id": "i"}),
        recorder=recorder,
    )
    assert result["error"] == "chain_depth_exceeded"
    assert result["chain_depth"] == _MAX_PEER_CHAIN_DEPTH + 1
    # Guard fires before any delivery — no POST was made.
    assert not any(r.method == "POST" for r in recorder)


@pytest.mark.asyncio
async def test_self_message_rejected() -> None:
    """A session cannot peer-message itself."""
    handler = _make_handler(items=[], post_response=httpx.Response(202))
    async with _client(handler) as client:
        out = await _execute_peer_message_tool(
            {"session_id": _SELF, "message": "hi"},
            server_client=client,
            conversation_id=_SELF,
        )
    assert json.loads(out)["error"] == "self_message"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"message": "hi"},  # missing session_id
        {"session_id": _TARGET},  # missing message
        {"session_id": _TARGET, "message": "   "},  # blank message
        {"session_id": "", "message": "hi"},  # empty session_id
    ],
)
async def test_rejects_bad_args(args: dict[str, object]) -> None:
    """Malformed arguments return an Error string, not a delivery."""
    handler = _make_handler(items=[], post_response=httpx.Response(202))
    async with _client(handler) as client:
        out = await _execute_peer_message_tool(args, server_client=client, conversation_id=_SELF)
    assert out.startswith("Error: sys_session_message")


@pytest.mark.asyncio
async def test_feature_disabled_surfaces_not_permitted() -> None:
    """A server 403 (flag off or no access) maps to not_permitted."""
    result = await _run(
        items=[_user_message_item()],
        post_response=httpx.Response(
            403, json={"detail": "cross-session messaging is not enabled on this deployment"}
        ),
    )
    assert result["error"] == "not_permitted"
    assert result["conversation_id"] == _TARGET


@pytest.mark.asyncio
async def test_unknown_target_maps_to_session_not_found() -> None:
    """A server 404 maps to session_not_found."""
    result = await _run(
        items=[_user_message_item()],
        post_response=httpx.Response(404, json={"error": "not found"}),
    )
    assert result["error"] == "session_not_found"


@pytest.mark.asyncio
async def test_inbound_read_failure_defaults_to_fresh_chain() -> None:
    """If the self-history read fails, the chain starts fresh (depth 1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith(f"/sessions/{_SELF}/items"):
            return httpx.Response(500)
        if request.method == "GET" and request.url.path.endswith(f"/sessions/{_SELF}"):
            return httpx.Response(200, json={"labels": {}})
        return httpx.Response(202, json={"item_id": "i"})

    async with _client(handler) as client:
        out = await _execute_peer_message_tool(
            {"session_id": _TARGET, "message": "hi"},
            server_client=client,
            conversation_id=_SELF,
        )
    assert json.loads(out)["chain_depth"] == 1
