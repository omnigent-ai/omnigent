"""Tests for API response gzip compression."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_large_api_json_is_gzipped(client: httpx.AsyncClient) -> None:
    """A large JSON response is gzipped when the client accepts it.

    ``/openapi.json`` is always present, needs no session/auth, and is well
    over the compression floor — a stable large-JSON probe for the middleware.
    """
    resp = await client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in resp.headers.get("vary", "").lower()
