"""Real HTTP MCP and OAuth refresh through the policy adapter and runner client."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore
from tests.e2e.test_mcp_registry import demo_mcp as demo_mcp
from tests.server.helpers import create_test_session
from tests.server.test_credential_store import _FakeCipher


@pytest.fixture
def app(runtime_init, db_uri, tmp_path, demo_mcp, monkeypatch):
    from omnigent.runtime import get_agent_cache, get_agent_store, get_conversation_store
    from omnigent.server.app import create_app
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv(
        "OMNIGENT_MCP_OAUTH_STATE_SECRET", "local-test-signing-key-32-characters-long"
    )
    entry = McpService(
        id="tracker",
        title="Tracker",
        url=demo_mcp + "/mcp",
        auth="oauth",
        allow_http=True,
        tools=["whoami", "read_ticket"],
        oauth={
            "authorize_url": demo_mcp + "/authorize",
            "token_url": demo_mcp + "/token",
            "client_id": "demo",
        },
    )
    store = CredentialStore(db_uri, _FakeCipher())
    registry = McpRegistry(
        McpRegistryConfig(public_url="http://localhost", services=[entry]), store
    )
    return create_app(
        agent_store=get_agent_store(),
        conversation_store=get_conversation_store(),
        agent_cache=get_agent_cache(),
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        file_store=SqlAlchemyFileStore(db_uri),
        mcp_registry=registry,
    )


@pytest.mark.asyncio
async def test_real_http_oauth_refresh_through_gateway(client, app, demo_mcp):
    store = app.state.mcp_registry.store
    start = await client.get("/v1/connections/mcp-tracker/connect")
    assert start.status_code in (302, 307), start.text
    fields = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    async with httpx.AsyncClient() as provider:
        consent = await provider.post(
            demo_mcp + "/authorize", data={**fields, "user": "alice@example.test"}
        )
    callback = await client.get(consent.headers["location"])
    assert "mcp-tracker=connected" in callback.headers["location"]
    session = await create_test_session(client, name="http-gateway")
    sid = session["id"]
    attached = await client.post(
        f"/v1/sessions/{sid}/agent/mcp-servers", json={"name": "tracker", "transport": "registry"}
    )
    assert attached.status_code == 200, attached.text
    spec = AgentSpec(
        spec_version=1,
        name="http-gateway",
        mcp_servers=[MCPServerConfig(name="tracker", transport="registry")],
    )
    proxy = ProxyMcpManager(sid, client)
    schemas = await proxy.schemas_for(spec)
    assert schemas.tool_names == {"tracker__whoami", "tracker__read_ticket"}
    grant = store.get("local", "mcp:tracker", with_secret=True)
    assert grant is not None
    store.update_secret("local", "mcp:tracker", secret=grant.secret, metadata={"expires_at": 0})
    result = await proxy.call_tool(spec, "tracker__whoami", {})
    assert "alice@example.test" in result
    assert '"refresh_count":1' in result.replace(" ", "")
    fresh = ProxyMcpManager(sid, client)
    assert "TEST-123" in await fresh.call_tool(
        spec, "tracker__read_ticket", {"ticket_id": "TEST-123"}
    )
    disconnected = await client.delete("/v1/mcp-registry/services/tracker/connection")
    assert disconnected.status_code == 200
    assert "Connect" in await proxy.call_tool(spec, "tracker__whoami", {})
