"""Session listing metadata and cursor forwarding through httpx.MockTransport.

Listings expose has_more, first_id and last_id; resolve_agent distinguishes
a stalled cursor from an exhausted listing."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from omnigent_client._sessions import SessionsNamespace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_namespace(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[SessionsNamespace, httpx.AsyncClient]:
    """Wire a :class:`SessionsNamespace` to a mock HTTP transport.

    :param handler: Per-request callable; receives the request and
        returns the response.
    :returns: The namespace and the underlying client (caller closes
        the client in ``finally``).
    """
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://srv")
    return SessionsNamespace(client, "http://srv"), client


# ---------------------------------------------------------------------------
# list_items pagination metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_items_returns_pagination_metadata() -> None:
    """Item pages expose has_more, first_id and last_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/items" in request.url.path
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "msg_001", "type": "message"},
                    {"id": "msg_002", "type": "message"},
                ],
                "has_more": True,
                "first_id": "msg_001",
                "last_id": "msg_002",
            },
        )

    ns, client = _make_namespace(handler)
    try:
        result = await ns.list_items("conv_abc", limit=2)
    finally:
        await client.aclose()

    # The fix must return something that exposes pagination metadata, not a
    # bare list.  A ``PaginatedList`` dataclass or similar with ``has_more``,
    # ``first_id``, ``last_id`` fields (and ``.data`` for the rows) is the
    # expected shape; a bare ``list`` is the pre-fix (broken) shape.
    assert not isinstance(result, list), (
        "list_items returned a bare list — pagination metadata (has_more, "
        "first_id, last_id) was discarded.  A caller reading a session with "
        "more than 'limit' items cannot detect that its view is a prefix."
    )
    assert hasattr(result, "has_more"), "list_items result has no 'has_more' attribute"
    assert result.has_more is True
    assert hasattr(result, "first_id") and result.first_id == "msg_001"
    assert hasattr(result, "last_id") and result.last_id == "msg_002"
    assert hasattr(result, "data") and len(result.data) == 2


@pytest.mark.asyncio
async def test_list_items_pagination_with_cursor() -> None:
    """Forward after=last_id so callers can fetch the next item page."""
    received_after: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_after.append(request.url.params.get("after"))
        return httpx.Response(
            200,
            json={
                "data": [{"id": "msg_003", "type": "message"}],
                "has_more": False,
                "first_id": "msg_003",
                "last_id": "msg_003",
            },
        )

    ns, client = _make_namespace(handler)
    try:
        await ns.list_items("conv_abc", limit=1, after="msg_002")
    finally:
        await client.aclose()

    assert received_after == ["msg_002"], (
        f"expected after='msg_002' in request params, got {received_after}"
    )


# ---------------------------------------------------------------------------
# child_sessions pagination metadata and after cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_sessions_returns_pagination_metadata() -> None:
    """Child-session pages expose has_more, first_id and last_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "child_sessions" in request.url.path
        return httpx.Response(
            200,
            json={
                "data": [{"id": "conv_child1", "title": "worker:task1"}],
                "has_more": True,
                "first_id": "conv_child1",
                "last_id": "conv_child1",
            },
        )

    ns, client = _make_namespace(handler)
    try:
        result = await ns.child_sessions("conv_parent", limit=1)
    finally:
        await client.aclose()

    assert not isinstance(result, list), (
        "child_sessions returned a bare list — pagination metadata (has_more, "
        "first_id, last_id) was discarded.  A parent with more than 'limit' "
        "children is silently truncated with no way to detect it."
    )
    assert hasattr(result, "has_more"), "child_sessions result has no 'has_more' attribute"
    assert result.has_more is True
    assert hasattr(result, "data") and len(result.data) == 1


@pytest.mark.asyncio
async def test_child_sessions_accepts_after_cursor() -> None:
    """Forward the child-session after cursor to make later pages reachable."""
    import inspect

    ns, client = _make_namespace(
        lambda req: httpx.Response(200, json={"data": [], "has_more": False})
    )
    try:
        sig = inspect.signature(ns.child_sessions)
        assert "after" in sig.parameters, (
            "child_sessions has no 'after' parameter — pagination past the "
            "first page of children is entirely unreachable.  A parent with "
            "more than 'limit' children can never be fully listed."
        )
    finally:
        await client.aclose()

    # Also confirm the value reaches the server.
    received_after: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_after.append(request.url.params.get("after"))
        return httpx.Response(
            200,
            json={"data": [], "has_more": False, "first_id": None, "last_id": None},
        )

    ns2, client2 = _make_namespace(handler)
    try:
        await ns2.child_sessions("conv_parent", limit=1, after="conv_child1")
    finally:
        await client2.aclose()

    assert received_after == ["conv_child1"], (
        f"expected after='conv_child1' forwarded to server, got {received_after}"
    )


# ---------------------------------------------------------------------------
# resolve_agent silent truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_agent_raises_on_has_more_without_cursor() -> None:
    """A missing cursor with has_more=True must raise a pagination error."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        # Page 1: has_more but no last_id — pagination cannot advance.
        return httpx.Response(
            200,
            json={
                "data": [{"id": "ag_1", "name": "other_agent", "harness": "claude-sdk"}],
                "has_more": True,
                "first_id": "ag_1",
                "last_id": None,  # absent cursor → pagination stall
            },
        )

    ns, client = _make_namespace(handler)
    try:
        with pytest.raises(Exception) as exc_info:
            await ns.resolve_agent("target_agent")
    finally:
        await client.aclose()

    # The bug: raises LookupError after a single request, silently treating
    # the stalled pagination as "not found".
    # The fix: raises a non-LookupError (e.g. OmnigentError or RuntimeError)
    # that indicates the pagination stall, so the caller can distinguish
    # "not found" from "couldn't finish listing".
    assert not isinstance(exc_info.value, LookupError), (
        "resolve_agent raised LookupError when has_more=True but no cursor was "
        "supplied — the agent on the next page is silently reported as missing.  "
        "The fix should raise a non-LookupError error to surface the stall."
    )
    # Confirm pagination was at least attempted (> 1 request), or that a
    # non-LookupError was raised on the stall condition.
    # Either the stall is detected immediately (1 request + non-LookupError)
    # or the client retried and then raised — both are acceptable; the only
    # forbidden outcome is silently returning LookupError.


@pytest.mark.asyncio
async def test_resolve_agent_raises_on_repeated_cursor() -> None:
    """A cursor that never advances must raise instead of paging forever."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count > 5:
            # Escape hatch so a regressed client fails fast instead of
            # spinning: end the listing and let it fall through to the
            # clean-miss LookupError the assertion below rejects.
            return httpx.Response(
                200,
                json={"data": [], "has_more": False, "first_id": None, "last_id": None},
            )
        # Every page reports more and hands back the same cursor.
        return httpx.Response(
            200,
            json={
                "data": [{"id": "ag_1", "name": "other_agent", "harness": "claude-sdk"}],
                "has_more": True,
                "first_id": "ag_1",
                "last_id": "ag_1",
            },
        )

    ns, client = _make_namespace(handler)
    try:
        with pytest.raises(Exception) as exc_info:
            await ns.resolve_agent("target_agent")
    finally:
        await client.aclose()

    assert not isinstance(exc_info.value, LookupError), (
        "resolve_agent looped on a non-advancing cursor and reported the "
        "stalled walk as a clean 'no such agent' miss"
    )
    assert request_count <= 2, (
        f"resolve_agent kept re-fetching the same page {request_count} times "
        "instead of raising on the repeated cursor"
    )
