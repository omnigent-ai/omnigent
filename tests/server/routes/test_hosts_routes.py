"""Tests for the hosts REST routes (``/v1/hosts``).

The hosts router is only mounted when ``host_store`` is provided to
``create_app``. The standard test ``app`` fixture does not supply one,
so host endpoints return 404. These tests verify the expected behavior
when hosts are not configured, and test the route helpers directly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_skills import request_host_skills


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
        monkeypatch.setattr("omnigent.server.routes._host_skills._SKILLS_TIMEOUT_S", 0.01)
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
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(HTTPException) as exc:
            await task
        assert exc.value.status_code == status
    assert conn.pending_skills == {}
