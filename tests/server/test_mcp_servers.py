"""Route tests for standalone MCP servers (``/v1/mcp-servers``)."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_create_list_get_delete_round_trip(client: httpx.AsyncClient) -> None:
    # Create.
    resp = await client.post(
        "/v1/mcp-servers",
        json={
            "name": "litellm",
            "transport": "http",
            "url": "https://gateway.example.com/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "description": "LiteLLM gateway",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "litellm"
    assert created["id"].startswith("mcp_")
    # Secret values never leave the server; only the keys do.
    assert created["header_keys"] == ["Authorization"]
    assert "headers" not in created

    server_id = created["id"]

    # List/mine.
    resp = await client.get("/v1/mcp-servers/mine")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [s["id"] for s in data] == [server_id]

    # Get one.
    resp = await client.get(f"/v1/mcp-servers/{server_id}")
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://gateway.example.com/mcp"

    # Delete.
    resp = await client.delete(f"/v1/mcp-servers/{server_id}")
    assert resp.status_code == 204
    resp = await client.get(f"/v1/mcp-servers/{server_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(client: httpx.AsyncClient) -> None:
    body = {"name": "dup", "transport": "http", "url": "https://a.example.com"}
    assert (await client.post("/v1/mcp-servers", json=body)).status_code == 201
    resp = await client.post("/v1/mcp-servers", json=body)
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_http_requires_url(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/mcp-servers",
        json={"name": "nourl", "transport": "http"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_stdio_requires_command(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/mcp-servers",
        json={"name": "nocmd", "transport": "stdio"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_replaces_config(client: httpx.AsyncClient) -> None:
    created = (
        await client.post(
            "/v1/mcp-servers",
            json={"name": "old", "transport": "http", "url": "https://old.example.com"},
        )
    ).json()
    resp = await client.put(
        f"/v1/mcp-servers/{created['id']}",
        json={"name": "new", "transport": "http", "url": "https://new.example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "new"
    assert resp.json()["url"] == "https://new.example.com"


@pytest.mark.asyncio
async def test_config_endpoint_returns_secrets_to_owner(client: httpx.AsyncClient) -> None:
    created = (
        await client.post(
            "/v1/mcp-servers",
            json={
                "name": "secretful",
                "transport": "http",
                "url": "https://gw.example.com",
                "headers": {"Authorization": "Bearer tok"},
            },
        )
    ).json()
    # The plain GET hides secret values...
    plain = (await client.get(f"/v1/mcp-servers/{created['id']}")).json()
    assert "headers" not in plain
    assert plain["header_keys"] == ["Authorization"]
    # ...but the explicit /config route returns them for agent baking.
    resp = await client.get(f"/v1/mcp-servers/{created['id']}/config")
    assert resp.status_code == 200, resp.text
    assert resp.json()["headers"] == {"Authorization": "Bearer tok"}


@pytest.mark.asyncio
async def test_config_missing_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/mcp-servers/mcp_nope/config")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_verify_validates_body(client: httpx.AsyncClient) -> None:
    # Bad transport shape is rejected before any connection attempt.
    resp = await client.post(
        "/v1/mcp-servers/verify",
        json={"transport": "stdio"},  # missing command
    )
    assert resp.status_code == 422
