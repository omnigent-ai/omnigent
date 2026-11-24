"""HTTP uploads retain validation even on the local single-user server."""

import json

import httpx
import pytest
from fastapi import FastAPI

from tests.server.helpers import create_test_agent
from tests.server.test_bundles import _MIN_CONFIG, _make_bundle_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "replace", "install"])
async def test_local_http_upload_rejects_source_tools(
    client: httpx.AsyncClient,
    route: str,
    app: FastAPI,
) -> None:
    agent = await create_test_agent(client, name="existing")
    session_id = agent["_session_id"]
    bundle = _make_bundle_bytes(
        {
            "config.yaml": _MIN_CONFIG.format(name="uploaded"),
            "tools/python/echo.py": "def echo(text: str) -> str:\n    return text\n",
        }
    )
    path = {
        "create": "/v1/sessions",
        "replace": f"/v1/sessions/{session_id}/agent",
        "install": "/v1/agents",
    }[route]
    response = await client.request(
        "PUT" if route == "replace" else "POST",
        path,
        data={"metadata": json.dumps({"parent_session_id": session_id})}
        if route == "create"
        else {},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert response.status_code == 400, response.text
    assert "may not ship a server-side Python/TS tool file" in response.text
    if route == "install":
        assert app.state.agent_store.list_user_agents("local").data == []
        return
    unchanged = await client.get(f"/v1/sessions/{session_id}/agent")
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["id"] == agent["id"]
