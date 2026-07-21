"""Integration tests for session MCP server management routes."""

from __future__ import annotations

import io
import tarfile
from typing import Any

import httpx
import pytest
import yaml

from omnigent.server.routes import session_mcp_servers as mcp_routes
from tests.server.helpers import create_test_session

pytestmark = pytest.mark.asyncio


async def test_create_mcp_server_updates_agent_bundle(client: httpx.AsyncClient) -> None:
    """POST creates an MCP YAML file and the session agent reports it."""
    session = await create_test_session(client, name="mcp-agent")
    session_id = session["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "github",
            "transport": "http",
            "url": "https://example.com/sse",
            "description": "GitHub tools",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "name": "github",
        "transport": "http",
        "description": "GitHub tools",
        "url": "https://example.com/sse",
        "headers": {},
        "command": None,
        "args": [],
    }

    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent_resp.status_code == 200, agent_resp.text
    assert agent_resp.json()["mcp_servers"] == [resp.json()]
    assert _mcp_file_from_bundle(
        await _agent_bundle(client, session_id),
        "github.yaml",
    ) == {
        "name": "github",
        "transport": "http",
        "description": "GitHub tools",
        "url": "https://example.com/sse",
    }


async def test_mcp_server_mutations_reset_bound_runner_agent_cache(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP server mutations invalidate stale runner-side agent caches."""
    calls: list[tuple[str, str, object]] = []

    async def _fake_reset(
        session_id: str,
        agent_id: str,
        runner_router: object,
    ) -> None:
        calls.append((session_id, agent_id, runner_router))

    monkeypatch.setattr(mcp_routes, "_reset_runner_session_agent_cache", _fake_reset)
    session = await create_test_session(client, name="mcp-reset-agent")
    session_id = session["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "echo",
            "transport": "stdio",
            "command": "python",
            "args": ["echo_server.py"],
        },
    )

    assert resp.status_code == 200, resp.text

    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/echo",
        json={
            "name": "echo-renamed",
            "transport": "stdio",
            "command": "python",
            "args": ["echo_server.py"],
        },
    )
    assert update.status_code == 200, update.text

    delete = await client.delete(f"/v1/sessions/{session_id}/agent/mcp-servers/echo-renamed")
    assert delete.status_code == 204, delete.text

    assert [(sid, aid) for sid, aid, _ in calls] == [
        (session_id, session["agent_id"]),
        (session_id, session["agent_id"]),
        (session_id, session["agent_id"]),
    ]
    assert all(runner_router is not None for _, _, runner_router in calls)


async def test_update_mcp_server_can_rename_and_change_transport(
    client: httpx.AsyncClient,
) -> None:
    """PUT replaces the existing declaration and validates transport fields."""
    session = await create_test_session(client, name="mcp-update-agent")
    session_id = session["id"]
    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "search", "transport": "http", "url": "https://example.com/sse"},
    )
    assert create.status_code == 200, create.text

    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/search",
        json={
            "name": "local-search",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-search"],
        },
    )

    assert update.status_code == 200, update.text
    assert update.json() == {
        "name": "local-search",
        "transport": "stdio",
        "description": None,
        "url": None,
        "headers": {},
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-search"],
    }
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert [server["name"] for server in agent_resp.json()["mcp_servers"]] == ["local-search"]


async def test_delete_mcp_server_removes_it_from_agent(client: httpx.AsyncClient) -> None:
    """DELETE removes the MCP declaration from the stored bundle."""
    session = await create_test_session(client, name="mcp-delete-agent")
    session_id = session["id"]
    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "github", "transport": "http", "url": "https://example.com/sse"},
    )
    assert create.status_code == 200, create.text

    delete = await client.delete(f"/v1/sessions/{session_id}/agent/mcp-servers/github")

    assert delete.status_code == 204, delete.text
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent_resp.status_code == 200, agent_resp.text
    assert agent_resp.json()["mcp_servers"] == []


async def test_create_mcp_server_rejects_duplicate_name(client: httpx.AsyncClient) -> None:
    """Creating the same MCP server twice returns 409."""
    session = await create_test_session(client, name="mcp-dup-agent")
    session_id = session["id"]
    payload = {"name": "github", "transport": "http", "url": "https://example.com/sse"}
    first = await client.post(f"/v1/sessions/{session_id}/agent/mcp-servers", json=payload)
    assert first.status_code == 200, first.text

    second = await client.post(f"/v1/sessions/{session_id}/agent/mcp-servers", json=payload)

    assert second.status_code == 409, second.text


async def test_create_mcp_server_supports_single_yaml_bundle(client: httpx.AsyncClient) -> None:
    """Single-file omnigent YAML bundles are updated inline."""
    create_session = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={
            "bundle": (
                "agent.tar.gz",
                _single_yaml_bundle(
                    """\
name: single_yaml_agent
prompt: Say hello.
executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents
"""
                ),
                "application/gzip",
            )
        },
    )
    assert create_session.status_code == 201, create_session.text
    session_id = create_session.json()["session_id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "browser-search", "transport": "http", "url": "https://example.com/sse"},
    )

    assert resp.status_code == 200, resp.text
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert [server["name"] for server in agent_resp.json()["mcp_servers"]] == ["browser-search"]


async def test_update_mcp_server_preserves_headers_on_redacted_roundtrip(
    client: httpx.AsyncClient,
) -> None:
    """Editing a server while sending [REDACTED] header values must not overwrite secrets."""
    session = await create_test_session(client, name="mcp-headers-agent")
    session_id = session["id"]

    # Create server with a real Authorization header.
    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse",
            "headers": {"Authorization": "Bearer real-token"},
        },
    )
    assert create.status_code == 200, create.text

    # Simulate the UI round-trip: the GET returns [REDACTED] values; the client
    # sends them back verbatim when editing only the URL.
    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/secure",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse-v2",
            "headers": {"Authorization": "[REDACTED]"},
        },
    )
    assert update.status_code == 200, update.text

    # The bundle must still contain the real token, not the sentinel.
    bundle = await _agent_bundle(client, session_id)
    mcp_file = _mcp_file_from_bundle(bundle, "secure.yaml")
    assert mcp_file["url"] == "https://example.com/sse-v2"
    assert mcp_file.get("headers") == {"Authorization": "Bearer real-token"}


async def test_update_mcp_server_clears_headers_when_empty_dict_sent(
    client: httpx.AsyncClient,
) -> None:
    """Sending headers={} on update must remove all headers from the bundle."""
    session = await create_test_session(client, name="mcp-clear-headers-agent")
    session_id = session["id"]

    create = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse",
            "headers": {"Authorization": "Bearer real-token"},
        },
    )
    assert create.status_code == 200, create.text

    # User removes all header rows — client sends {}.
    update = await client.put(
        f"/v1/sessions/{session_id}/agent/mcp-servers/secure",
        json={
            "name": "secure",
            "transport": "http",
            "url": "https://example.com/sse",
            "headers": {},
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["headers"] == {}

    bundle = await _agent_bundle(client, session_id)
    mcp_file = _mcp_file_from_bundle(bundle, "secure.yaml")
    assert "headers" not in mcp_file


async def _agent_bundle(client: httpx.AsyncClient, session_id: str) -> bytes:
    """Download the session agent bundle."""
    resp = await client.get(f"/v1/sessions/{session_id}/agent/contents")
    assert resp.status_code == 200, resp.text
    return resp.content


def _mcp_file_from_bundle(bundle: bytes, filename: str) -> dict[str, Any]:
    """Read one MCP YAML file from a bundle by basename."""
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith(f"/tools/mcp/{filename}"))
        extracted = tf.extractfile(member)
        assert extracted is not None
        data = yaml.safe_load(extracted.read())
    assert isinstance(data, dict)
    return data


def _single_yaml_bundle(yaml_text: str) -> bytes:
    """Build a tar.gz bundle containing one omnigent YAML file."""
    buf = io.BytesIO()
    data = yaml_text.encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="agent.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()
