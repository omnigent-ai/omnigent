"""Tests for the hosts REST routes (``/v1/hosts``).

The hosts router is only mounted when ``host_store`` is provided to
``create_app``. The standard test ``app`` fixture does not supply one,
so host endpoints return 404. These tests verify the expected behavior
when hosts are not configured, and test the route helpers directly.
"""

from __future__ import annotations

import httpx


async def test_hosts_not_mounted_without_host_store(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts returns 404 when hosts are not configured."""
    resp = await client.get("/v1/hosts")
    # When host_store is not provided, the router is not mounted at all.
    assert resp.status_code == 404


async def test_get_host_not_mounted(client: httpx.AsyncClient) -> None:
    """GET /v1/hosts/{id} returns 404 when hosts are not configured."""
    resp = await client.get("/v1/hosts/host_nonexistent_12345")
    assert resp.status_code == 404
