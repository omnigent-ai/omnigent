"""The SDK sub-agent tree walk against the real sessions routes and store.

``SessionsNamespace.child_sessions_tree`` feeds the CLI ``↓`` navigator, the
REPL poll and ``subtree_busy``. These tests drive it through the in-process app
so the request count and the returned descendants reflect the real
``GET /v1/sessions/{id}/descendants`` contract rather than a canned handler.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from omnigent_client._sessions import SessionsNamespace

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_root(client: httpx.AsyncClient, agent_name: str) -> dict[str, Any]:
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_child(
    client: httpx.AsyncClient, agent_id: str, parent_id: str, title: str
) -> str:
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "parent_session_id": parent_id, "title": title},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@contextmanager
def _recorded_requests(client: httpx.AsyncClient) -> Iterator[list[str]]:
    """Collect every request URL the client sends inside the block."""
    seen: list[str] = []

    async def record(request: httpx.Request) -> None:
        seen.append(str(request.url))

    previous = client.event_hooks
    client.event_hooks = {**previous, "request": [*previous.get("request", []), record]}
    try:
        yield seen
    finally:
        client.event_hooks = previous


async def test_child_sessions_tree_request_count_is_independent_of_tree_size(
    client: httpx.AsyncClient,
) -> None:
    """Fetching a whole subtree must not cost one HTTP round trip per node."""
    root = await _create_root(client, "tree-walk-fanout")
    expected: set[str] = set()
    for ci in range(3):
        child = await _create_child(client, root["agent_id"], root["id"], f"worker:c{ci}")
        expected.add(child)
        for gi in range(2):
            grandchild = await _create_child(client, root["agent_id"], child, f"worker:c{ci}g{gi}")
            expected.add(grandchild)

    with _recorded_requests(client) as requests:
        nodes = await SessionsNamespace(client, "http://test").child_sessions_tree(root["id"])

    assert {n["id"] for n in nodes} == expected
    assert len(requests) <= 2, (
        f"{len(requests)} requests for a {len(expected)}-node tree: "
        "the walk still fetches one page per node"
    )


async def test_child_sessions_tree_lists_more_than_one_page_of_children(
    client: httpx.AsyncClient,
) -> None:
    """A parent with more children than one page must not lose the rest silently."""
    root = await _create_root(client, "tree-walk-wide")
    expected = {
        await _create_child(client, root["agent_id"], root["id"], f"worker:m{i:03d}")
        for i in range(101)
    }

    nodes = await SessionsNamespace(client, "http://test").child_sessions_tree(root["id"])

    assert {n["id"] for n in nodes} == expected, (
        f"{len(nodes)} of {len(expected)} children returned"
    )


async def test_child_sessions_tree_descends_through_archived_intermediate(
    client: httpx.AsyncClient,
) -> None:
    """Archiving a middle node must not hide the live sessions underneath it."""
    root = await _create_root(client, "tree-walk-archived")
    middle = await _create_child(client, root["agent_id"], root["id"], "worker:middle")
    leaf = await _create_child(client, root["agent_id"], middle, "worker:leaf")
    resp = await client.patch(f"/v1/sessions/{middle}", json={"archived": True})
    assert resp.status_code == 200, resp.text

    nodes = await SessionsNamespace(client, "http://test").child_sessions_tree(root["id"])

    by_id = {n["id"]: n for n in nodes}
    assert leaf in by_id, f"live descendant under an archived node is missing; got {sorted(by_id)}"
    assert by_id[leaf]["parent_id"] == middle
    # The archived parent stays, flagged, so the tree the REPL renders is connected.
    assert by_id[middle]["archived"] is True
