"""Real OAuth and HTTP MCP round trip without an LLM or external accounts."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.server.routes.mcp_registry import create_mcp_registry_router
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore
from tests.server.test_credential_store import _FakeCipher


@pytest.fixture
def demo_mcp(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[2]
    with (tmp_path / "demo.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(root / "examples/mcp-registry/demo_service.py"),
                "--port",
                str(port),
                "--redirect-uri",
                "http://localhost/v1/connections/mcp-tracker/callback",
            ],
            stdout=log,
            stderr=log,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                try:
                    if httpx.get(url + "/health", timeout=0.2).status_code == 200:
                        break
                except httpx.HTTPError:
                    # Connection/read failures are expected while the server starts.
                    pass
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                time.sleep(0.1)
            else:
                pytest.fail("Demo MCP service did not start")
            yield url
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.asyncio
async def test_connect_refresh_reuse_and_disconnect(db_uri, demo_mcp, monkeypatch):
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
    app = FastAPI()
    app.state.mcp_registry_auth = None
    app.include_router(create_mcp_registry_router(registry, None), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as browser:
        start = await browser.get("/v1/connections/mcp-tracker/connect")
        assert start.status_code in {302, 307}
        fields = {
            key: values[0]
            for key, values in parse_qs(urlsplit(start.headers["location"]).query).items()
        }
        fields["user"] = "alice@example.test"
        async with httpx.AsyncClient() as provider:
            consent = await provider.post(demo_mcp + "/authorize", data=fields)
        callback = await browser.get(consent.headers["location"])
        assert "mcp-tracker=connected" in callback.headers["location"]
        catalog = await browser.get("/v1/mcp-registry/services")
        assert catalog.json()["data"][0]["connected"]
        test = await browser.post("/v1/mcp-registry/services/tracker/test")
        assert test.json() == {"tools": ["whoami", "read_ticket"]}, test.text
        # Force the real provider refresh path without waiting for the token TTL.
        connection = store.get("local", "mcp:tracker", with_secret=True)
        assert connection is not None
        store.update_secret(
            "local", "mcp:tracker", secret=connection.secret, metadata={"expires_at": 0}
        )
        result = await registry.execute("tracker", "local", app.state, tool="whoami")
        assert (
            '"refresh_count": 1' in result.content[0].text
            or '"refresh_count":1' in result.content[0].text
        )
        # A new gateway instance / fresh execution host reuses the stored account.
        replacement = McpRegistry(registry.config, store)
        result = await replacement.execute(
            "tracker", "local", app.state, tool="read_ticket", arguments={"ticket_id": "TEST-123"}
        )
        assert "TEST-123" in result.content[0].text
        with pytest.raises(ConnectionError, match="not allowed"):
            await registry.execute("tracker", "local", app.state, tool="delete_ticket")
        await browser.delete("/v1/mcp-registry/services/tracker/connection")
        with pytest.raises(ConnectionError, match="Connect"):
            await replacement.execute("tracker", "local", app.state, tool="whoami")
        assert (await browser.get("/v1/mcp-registry/services")).json()["data"][0][
            "connected"
        ] is False


def test_demo_authorization_requires_registered_callback(demo_mcp):
    response = httpx.post(
        demo_mcp + "/authorize",
        data={
            "redirect_uri": "http://localhost/unregistered-callback",
            "state": "test-state",
            "code_challenge_method": "S256",
            "code_challenge": "test-challenge",
        },
    )
    assert response.status_code == 400
    assert "location" not in response.headers
