"""Regression tests for the sessions namespace pagination metadata contract.

``list_items`` and ``child_sessions`` in :class:`SessionsNamespace`
return a bare ``list`` and silently discard ``has_more``, ``first_id``,
and ``last_id`` from the server response.  A caller that reads a session
transcript beyond ``limit`` rows has no way to know its view is a
prefix.  ``child_sessions`` also accepts no ``after`` parameter, so
a listing past ``limit`` children is unreachable, not merely unobservable.

A secondary issue (also from the report) is that ``resolve_agent``
terminates quietly when the server reports ``has_more`` but supplies no
``last_id``: the agent is silently reported as missing even though it
exists on an unreached page.

What each test claims to prove and what a failure means
-------------------------------------------------------

* ``test_list_items_returns_pagination_metadata``: ``list_items`` must
  surface ``has_more`` / ``first_id`` / ``last_id`` from the server
  response.  Failure means callers cannot detect a truncated listing.

* ``test_list_items_pagination_with_cursor``: ``list_items`` must accept
  an ``after`` cursor and forward it to the server, so a caller can
  walk page 2.  Failure means the paging contract is broken on the
  request side.

* ``test_child_sessions_returns_pagination_metadata``: ``child_sessions``
  must surface ``has_more`` / ``first_id`` / ``last_id``.  Failure means
  callers cannot detect truncated child listings.

* ``test_child_sessions_accepts_after_cursor``: ``child_sessions`` must
  accept an ``after`` parameter and forward it to the server.  Failure
  means a listing past ``limit`` children is entirely unreachable.

* ``test_resolve_agent_raises_on_has_more_without_cursor``: when the
  server reports ``has_more=True`` but omits ``last_id`` (preventing
  cursor advance), ``resolve_agent`` must raise rather than silently
  return ``LookupError`` for an agent that might exist on the next page.
  Failure means a pagination-stall is indistinguishable from
  "agent does not exist".

Mocks at the HTTP transport boundary via :class:`httpx.MockTransport`;
no live server required.
"""

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
    """``list_items`` must expose ``has_more``, ``first_id``, and ``last_id``.

    The server always returns these fields (``paginate_in_memory`` in
    ``omnigent/entities/pagination.py`` computes them for every listing).
    Silently dropping them prevents the caller from knowing that the
    returned list is a prefix of the full conversation — a session with
    more than ``limit`` items (default 100) is silently truncated.

    Failure means the return type is a bare ``list`` with no pagination
    metadata, confirming the regression.
    """

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
    """``list_items`` forwards the ``after`` cursor to the server.

    A caller that receives ``has_more=True`` and a ``last_id`` must be
    able to fetch the next page by passing ``after=last_id``.  This test
    confirms that the client sends ``?after=<id>`` in the query string.
    Failure means the cursor is silently dropped and second-page fetches
    always return from the start.
    """
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
    """``child_sessions`` must expose ``has_more``, ``first_id``, and ``last_id``.

    When a parent session has more children than ``limit`` (default 100),
    silently dropping ``has_more`` from the response means the caller
    cannot detect that the child listing is incomplete — the sub-agent
    tree shown in the CLI and web Agents rail may be silently truncated.

    Failure means the return type is a bare ``list`` with no pagination
    metadata, confirming the regression.
    """

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
    """``child_sessions`` must accept an ``after`` cursor parameter.

    Without ``after``, a listing past the first ``limit`` children is
    entirely unreachable — there is no way to fetch page 2 at all,
    regardless of whether ``has_more`` is surfaced.  This test confirms
    that the method signature includes ``after`` and that the value is
    forwarded to the server query string.

    Failure means the parameter does not exist on the method, confirming
    the second regression.
    """
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
    """``resolve_agent`` must raise when ``has_more=True`` but no cursor is supplied.

    If the server reports ``has_more=True`` but omits ``last_id``
    (preventing cursor advance), the current code silently stops and
    returns ``LookupError("No agent named …")`` — which is wrong when
    the agent exists on an unreached page.  The fix must raise a
    descriptive error (not ``LookupError``) to distinguish "pagination
    stalled" from "agent genuinely not found".

    Failure (the bug) looks like: ``LookupError`` is raised and only one
    request is made — the agent on page 2 is never fetched.
    """
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
