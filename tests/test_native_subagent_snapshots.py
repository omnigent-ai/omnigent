"""Native inventory is delivery/liveness evidence, never a task outcome."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from omnigent.native.subagent_snapshot import (
    NativeSubagentSnapshotPublisher,
    parse_native_subagent_snapshot,
)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("sequence", 0),
        ("complete", "yes"),
        ("children", [{"session_id": "x", "status": "fake"}]),
        ("children", [{"session_id": "x", "status": "running"}] * 2),
    ],
)
def test_invalid_inventory_is_rejected(field: str, value: object) -> None:
    payload: dict[str, object] = {"generation": 1, "sequence": 1, "children": []}
    payload[field] = value
    with pytest.raises(ValueError):
        parse_native_subagent_snapshot(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [{}, {"child": "completed"}])
@pytest.mark.parametrize("failure", ["transport", 408, 429, 503])
async def test_final_retry_then_negative_ack(final: dict[str, str], failure: str | int) -> None:
    posts, acknowledged = [], asyncio.Event()

    def transport(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            if failure == "transport":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(int(failure))
        acknowledged.set()
        return httpx.Response(202, json={"accepted": False})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client, retry_s=0.005) as publisher:
            if not final:
                publisher.update("parent", {"child": "running"})
            publisher.update("parent", final)
            await asyncio.wait_for(acknowledged.wait(), 1)
            await asyncio.sleep(0.015)
    assert len(posts) == 2
    assert posts[0]["data"]["children"] == posts[1]["data"]["children"]
    assert posts[1]["data"]["sequence"] > posts[0]["data"]["sequence"]


@pytest.mark.asyncio
async def test_retirement_retries_and_does_not_retain_old_parent() -> None:
    posts = []
    done = asyncio.Event()

    def transport(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["data"])
        if len(posts) == 1:
            return httpx.Response(503)
        done.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client, retry_s=0.005) as publisher:
            publisher.update("old", {}, retired=True)
            await asyncio.wait_for(done.wait(), 1)
            await asyncio.sleep(0)
            assert "old" not in publisher._inventories
    assert all(post["retired"] for post in posts)


@pytest.mark.asyncio
async def test_old_server_disables_optional_snapshots() -> None:
    count = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(400, json={"error": {"message": "Unknown event type: snapshot"}})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client, heartbeat_s=0.005) as publisher:
            publisher.update("parent", {"child": "running"})
            await asyncio.sleep(0.02)
            publisher.update("parent", {"child": "completed"})
            await asyncio.sleep(0.01)
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403])
async def test_permanent_parent_rejection_is_logged_and_dropped_once(status: int, caplog) -> None:
    called, reconnected, count = asyncio.Event(), asyncio.Event(), 0

    def transport(_request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            called.set()
            return httpx.Response(status, json={"error": {"message": "invalid snapshot"}})
        reconnected.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        with caplog.at_level("WARNING"):
            async with NativeSubagentSnapshotPublisher(client, retry_s=0.005) as publisher:
                publisher.update("parent", {"child": "running"})
                await asyncio.wait_for(called.wait(), 1)
                await asyncio.sleep(0.02)
                assert "parent" not in publisher._inventories
        assert count == 1
        async with NativeSubagentSnapshotPublisher(client) as publisher:
            publisher.update("parent", {"child": "running"})
            await asyncio.wait_for(reconnected.wait(), 1)
    assert count == 2
    assert f"rejected parent parent with HTTP {status}" in caplog.text


@pytest.mark.asyncio
async def test_retired_404_does_not_disable_current_parent() -> None:
    posts, current_posted = [], asyncio.Event()

    def transport(request: httpx.Request) -> httpx.Response:
        parent = request.url.path.split("/")[-2]
        data = json.loads(request.content)["data"]
        posts.append((parent, data["children"], data["retired"]))
        if parent == "old":
            return httpx.Response(404)
        current_posted.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client) as publisher:
            publisher.update("old", {"stale": "running"}, retired=True)
            publisher.update("current", {"child": "running"})
            await asyncio.wait_for(current_posted.wait(), 1)
    assert not publisher._disabled
    assert posts == [
        ("old", [], True),
        ("current", [{"session_id": "child", "status": "running"}], False),
    ]


@pytest.mark.asyncio
async def test_current_parent_displaces_oldest_retirement_at_capacity() -> None:
    client = httpx.AsyncClient(base_url="http://test")
    publisher = NativeSubagentSnapshotPublisher(client)
    for index in range(64):
        publisher.update(f"old-{index}", {}, retired=True)
    publisher.update("current", {"child": "running"})
    await client.aclose()
    assert len(publisher._inventories) == 64
    assert publisher._inventories["current"].children == (("child", "running"),)


@pytest.mark.asyncio
async def test_oversized_inventory_is_explicitly_partial() -> None:
    posted = asyncio.Event()
    payload = {}

    def transport(request: httpx.Request) -> httpx.Response:
        payload.update(json.loads(request.content)["data"])
        posted.set()
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(transport)
    ) as client:
        async with NativeSubagentSnapshotPublisher(client) as publisher:
            publisher.update("parent", {str(i): "running" for i in range(513)})
            await asyncio.wait_for(posted.wait(), 1)
    snapshot = parse_native_subagent_snapshot(payload)
    assert not snapshot.complete
    assert len(snapshot.children) == 512


def test_codex_rotation_excludes_old_parent_children() -> None:
    from omnigent.harnesses.codex_native.forwarder import (
        _codex_native_subagent_snapshot,
        _CodexForwarderState,
    )

    state = _CodexForwarderState(parent_session_id="old")
    state.note_child_thread("t1", "c1")
    state.note_parent_rotation("new")
    state.note_child_thread("t2", "c2")
    assert state.session_for_child_thread("t1") == "c1"
    assert _codex_native_subagent_snapshot(state) == {"c2": "running"}


def test_quiescence_is_not_a_terminal_outcome() -> None:
    idle = parse_native_subagent_snapshot(
        {"generation": 1, "sequence": 1, "children": [{"session_id": "c", "status": "idle"}]}
    )
    assert (idle.children, idle.active_child_ids) == ({"c": "idle"}, frozenset())
