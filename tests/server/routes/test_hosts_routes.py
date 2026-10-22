"""Tests for the hosts REST routes (``/v1/hosts``).

The hosts router is only mounted when ``host_store`` is provided to
``create_app``. The standard test ``app`` fixture does not supply one,
so host endpoints return 404. These tests verify the expected behavior
when hosts are not configured, and test the route helpers directly.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent.host.frames import CAP_MCP_INVENTORY, HostHelloFrame
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.mcp_servers import request_host_mcp_servers
from omnigent.server.routes.skills import request_host_skills


async def test_hosts_not_mounted_without_host_store(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts returns 404 when hosts are not configured."""
    resp = await client.get("/v1/hosts")
    # When host_store is not provided, the router is not mounted at all.
    assert resp.status_code == 404


async def test_get_host_not_mounted(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts/{id} returns 404 when hosts are not configured."""
    resp = await client.get("/v1/hosts/host_nonexistent_12345")
    assert resp.status_code == 404


@pytest.mark.parametrize("outcome,status", [("timeout", 504), ("replaced", 502), ("cancel", None)])
async def test_skills_proxy_cleans_up_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int | None
) -> None:
    registry = HostRegistry()
    conn = registry.register(
        host_id="host_skills_test",
        ws=AsyncMock(),
        hello=HostHelloFrame(version="test", frame_protocol_version=1, name="test"),
        owner=None,
    )
    if outcome == "timeout":
        monkeypatch.setattr("omnigent.server.routes.skills._SKILLS_TIMEOUT_S", 0.01)
    elif outcome == "replaced":
        registry.deregister(conn.host_id)
    task = asyncio.create_task(
        request_host_skills(
            host_registry=registry, host_conn=conn, harness="claude-native", path="~"
        )
    )
    if outcome == "cancel":
        await asyncio.wait_for(conn.outbound_queue.get(), timeout=1)
        assert conn.pending_skills
        task.cancel()
    (result,) = await asyncio.gather(task, return_exceptions=True)
    if outcome == "cancel":
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert isinstance(result, HTTPException)
        assert result.status_code == status
    assert conn.pending_skills == {}


async def test_inventory_proxies_fail_fast_when_connection_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tunnel drop returns 502 for both inventory requests immediately."""
    monkeypatch.setattr("omnigent.server.routes.skills._SKILLS_TIMEOUT_S", 30.0)
    monkeypatch.setattr("omnigent.server.routes.mcp_servers._MCP_SERVERS_TIMEOUT_S", 30.0)
    registry = HostRegistry()
    conn = registry.register(
        host_id="host_inventory_test",
        ws=AsyncMock(),
        hello=HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="test",
            capabilities=[CAP_MCP_INVENTORY],
        ),
        owner=None,
    )
    skills_task = asyncio.create_task(
        request_host_skills(
            host_registry=registry,
            host_conn=conn,
            harness="claude-native",
            path="~",
        )
    )
    mcp_task = asyncio.create_task(
        request_host_mcp_servers(host_registry=registry, host_conn=conn)
    )
    while not conn.pending_skills or not conn.pending_mcp_servers:
        await asyncio.sleep(0)

    registry.deregister(conn.host_id)
    results = await asyncio.wait_for(
        asyncio.gather(skills_task, mcp_task, return_exceptions=True),
        timeout=1.0,
    )

    for result in results:
        assert isinstance(result, HTTPException)
        assert result.status_code == 502
    assert conn.pending_skills == {}
    assert conn.pending_mcp_servers == {}


@pytest.mark.parametrize("teardown", ["deregister", "replace"])
async def test_inventory_waiters_on_worker_loop_fail_from_foreign_teardown(
    monkeypatch: pytest.MonkeyPatch, teardown: str
) -> None:
    """A foreign-loop teardown wakes both inventory waiters without a timeout."""
    monkeypatch.setattr("omnigent.server.routes.skills._SKILLS_TIMEOUT_S", 0.5)
    monkeypatch.setattr("omnigent.server.routes.mcp_servers._MCP_SERVERS_TIMEOUT_S", 0.5)
    registry = HostRegistry()
    conn = registry.register(
        host_id=f"host_inventory_worker_{teardown}",
        ws=AsyncMock(),
        hello=HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="test",
            capabilities=[CAP_MCP_INVENTORY],
        ),
        owner=None,
    )
    ready = threading.Event()
    finished = threading.Event()
    results: list[object] = []

    def run_requests() -> None:
        async def requests() -> None:
            skills_task = asyncio.create_task(
                request_host_skills(
                    host_registry=registry,
                    host_conn=conn,
                    harness="claude-native",
                    path="~",
                )
            )
            mcp_task = asyncio.create_task(
                request_host_mcp_servers(host_registry=registry, host_conn=conn)
            )
            while not conn.pending_skills or not conn.pending_mcp_servers:
                await asyncio.sleep(0)
            ready.set()
            results.extend(await asyncio.gather(skills_task, mcp_task, return_exceptions=True))
            finished.set()

        asyncio.run(requests())

    worker = threading.Thread(target=run_requests, name="inventory-owner-loop")
    worker.start()
    assert await asyncio.to_thread(ready.wait, 1.0)
    if teardown == "deregister":
        registry.deregister(conn.host_id)
    else:
        registry.register(
            conn.host_id,
            AsyncMock(),
            HostHelloFrame(
                version="test",
                frame_protocol_version=1,
                name="replacement",
                capabilities=[CAP_MCP_INVENTORY],
            ),
            owner=None,
        )
    assert await asyncio.to_thread(finished.wait, 1.0)
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(results) == 2
    for result in results:
        assert isinstance(result, HTTPException)
        assert result.status_code == 502
