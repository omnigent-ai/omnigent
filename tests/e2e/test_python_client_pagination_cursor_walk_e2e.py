"""The Python client must surface listing truncation instead of swallowing it.

Drives ``SessionsNamespace`` against a LIVE server holding a transcript longer
than the requested page and a parent with more children than the requested
page. The server reports ``has_more``/``last_id`` on every listing
(``paginate_in_memory``); the client must let a caller observe truncation and
reach the next page. The stalled-listing case (``has_more`` with no cursor)
cannot be produced by the real server, so that one response is scripted.
"""

from __future__ import annotations

import asyncio
import builtins
import uuid
from typing import Any

import httpx
from omnigent_client._sessions import SessionsNamespace

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import lookup_agent_id


def _page(result: object) -> tuple[builtins.list[dict[str, Any]], Any, Any]:
    """Rows + (has_more, last_id) from whatever shape the client returns.

    A bare ``list`` is the broken shape — rows with the pagination metadata
    dropped — so both signals come back ``None``.
    """
    if isinstance(result, builtins.list):
        return result, None, None
    if isinstance(result, dict):
        return (
            builtins.list(result.get("data") or []),
            result.get("has_more"),
            result.get("last_id"),
        )
    return (
        builtins.list(getattr(result, "data", None) or []),
        getattr(result, "has_more", None),
        getattr(result, "last_id", None),
    )


def _import_four_item_session(http_client: httpx.Client) -> str:
    """Import a session whose transcript has 4 message items; return its id."""
    turns = [
        ("user", "input_text", "first question"),
        ("assistant", "output_text", "first answer"),
        ("user", "input_text", "second question"),
        ("assistant", "output_text", "second answer"),
    ]
    items = []
    for i, (role, kind, text) in enumerate(turns):
        data: dict[str, Any] = {"role": role, "content": [{"type": kind, "text": text}]}
        if role == "assistant":
            data["agent"] = "claude-native-ui"
        items.append({"type": "message", "response_id": f"claude:turn-{i // 2}", "data": data})
    resp = http_client.post(
        "/v1/imports",
        json={
            "source": "claude",
            "external_session_id": f"sdk-pagination-{uuid.uuid4().hex}",
            "workspace": "/repo/sdk-pagination",
            "items": items,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["session_id"])


def _create_session(client: httpx.Client, *, agent_id: str, parent_id: str | None = None) -> str:
    body: dict[str, Any] = {"agent_id": agent_id}
    if parent_id is not None:
        body["parent_session_id"] = parent_id
    resp = client.post("/v1/sessions", json=body, headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN})
    resp.raise_for_status()
    return str(resp.json()["id"])


def test_list_items_surfaces_truncation_and_walks_the_cursor(
    live_server: str,
    http_client: httpx.Client,
) -> None:
    """A limit=2 read of a 4-item transcript must be observably truncated."""
    session_id = _import_four_item_session(http_client)

    async def _drive() -> None:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            ns = SessionsNamespace(ac, live_server)

            first = await ns.list_items(session_id, limit=2)
            rows, has_more, last_id = _page(first)
            assert len(rows) == 2, f"expected a 2-row page, got {len(rows)}"
            assert has_more is True, (
                "list_items dropped the server's has_more — a truncated "
                "listing is indistinguishable from a complete one"
            )
            assert last_id, "list_items dropped the server's last_id cursor"

            second = await ns.list_items(session_id, limit=2, after=str(last_id))
            rows2, has_more2, _ = _page(second)
            seen = {r["id"] for r in rows} | {r["id"] for r in rows2}
            assert len(seen) == 4, f"cursor walk did not reach all 4 items: {seen}"
            assert not has_more2, "the second page of 4 items must be the last"

    asyncio.run(_drive())


def test_child_sessions_surfaces_truncation_and_reaches_page_two(
    live_server: str,
    http_client: httpx.Client,
    archer_agent: str,
) -> None:
    """A limit=1 read of a 2-child parent must expose and reach the second child."""
    agent_id = lookup_agent_id(http_client, archer_agent)
    parent = _create_session(http_client, agent_id=agent_id)
    children = {
        _create_session(http_client, agent_id=agent_id, parent_id=parent) for _ in range(2)
    }

    async def _drive() -> None:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            ns = SessionsNamespace(ac, live_server)

            first = await ns.child_sessions(parent, limit=1)
            rows, has_more, last_id = _page(first)
            assert len(rows) == 1, f"expected a 1-row page, got {len(rows)}"
            assert has_more is True, (
                "child_sessions dropped the server's has_more — the second "
                "child is invisible to the caller"
            )
            assert last_id, "child_sessions dropped the server's last_id cursor"

            second = await ns.child_sessions(parent, limit=1, after=str(last_id))
            rows2, _, _ = _page(second)
            seen = {r["id"] for r in rows} | {r["id"] for r in rows2}
            assert seen == children, f"cursor walk did not reach both children: {seen}"

    asyncio.run(_drive())


def test_resolve_agent_raises_on_stalled_listing() -> None:
    """``has_more`` with no cursor must raise, not report a clean miss."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "a1", "object": "agent", "name": "decoy", "harness": None}],
                "first_id": "a1",
                "last_id": None,
                "has_more": True,
            },
        )

    async def _drive() -> BaseException | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as stub:
            ns = SessionsNamespace(stub, "http://stalled.test")
            try:
                await ns.resolve_agent("needle")
            except Exception as exc:
                return exc
        return None

    exc = asyncio.run(_drive())
    assert exc is not None, "resolve_agent answered from a listing it knows is incomplete"
    assert not isinstance(exc, LookupError), (
        f"stalled listing reported as a clean 'no such agent' miss: {exc}"
    )
