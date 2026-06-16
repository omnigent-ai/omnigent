"""Tests for the policy registry route (``GET /v1/policy-registry``)."""

from __future__ import annotations

import httpx


async def test_list_policy_registry(client: httpx.AsyncClient) -> None:
    """GET /v1/policy-registry returns a list of registered handlers."""
    resp = await client.get("/v1/policy-registry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


async def test_policy_registry_entry_shape(client: httpx.AsyncClient) -> None:
    """Each registry entry has handler, description, and params_schema fields."""
    resp = await client.get("/v1/policy-registry")
    assert resp.status_code == 200
    entries = resp.json()["data"]
    if entries:  # registry may be empty if no policy modules are loaded
        for entry in entries:
            assert "handler" in entry
            assert "description" in entry
            assert "params_schema" in entry
