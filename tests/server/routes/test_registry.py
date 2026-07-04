"""Tests for the community agent registry routes (``/v1/registry``).

Uses a lightweight FastAPI app built from :func:`create_registry_router`
directly, backed by a real SQLite database, to test request/response
behaviour without needing the full runtime stack from the shared
``app`` / ``client`` fixtures.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.server.routes.registry import create_registry_router
from omnigent.stores.registry_store.sqlalchemy_store import SqlAlchemyRegistryStore

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def registry_store(db_uri: str) -> SqlAlchemyRegistryStore:
    """A registry store backed by the per-test SQLite DB."""
    return SqlAlchemyRegistryStore(db_uri)


@pytest.fixture()
def registry_app(registry_store: SqlAlchemyRegistryStore) -> FastAPI:
    """Minimal FastAPI app with only the registry router mounted."""
    app = FastAPI()
    app.include_router(
        create_registry_router(registry_store),
        prefix="/v1",
    )
    return app


@pytest_asyncio.fixture()
async def client(registry_app: FastAPI) -> httpx.AsyncClient:
    """Async HTTP client wired to the registry app (no real server)."""
    transport = httpx.ASGITransport(app=registry_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _publish_payload(**overrides: object) -> dict:
    defaults: dict = {
        "name": "my-agent",
        "version": "1.0.0",
        "harness": "claude-sdk",
        "description": "A handy agent.",
        "author": "alice@example.com",
    }
    defaults.update(overrides)
    return defaults


# ── GET /v1/registry (browse) ─────────────────────────────────────────────────


async def test_browse_empty(client: httpx.AsyncClient) -> None:
    """Browse returns an empty list when no agents have been published."""
    resp = await client.get("/v1/registry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


async def test_browse_returns_published_agents(client: httpx.AsyncClient) -> None:
    """After publishing, browse returns the entry."""
    await client.post("/v1/registry", json=_publish_payload())
    resp = await client.get("/v1/registry")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_browse_filter_by_harness(client: httpx.AsyncClient) -> None:
    """browse ``harness=`` filter returns only matching entries."""
    await client.post("/v1/registry", json=_publish_payload(name="a1", harness="claude-sdk"))
    await client.post(
        "/v1/registry",
        json=_publish_payload(name="a2", version="1.0.0", harness="codex"),
    )
    resp = await client.get("/v1/registry?harness=codex")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["harness"] == "codex"


async def test_browse_keyword_search(client: httpx.AsyncClient) -> None:
    """browse ``q=`` matches on name."""
    await client.post("/v1/registry", json=_publish_payload(name="typescript-helper"))
    await client.post(
        "/v1/registry",
        json=_publish_payload(name="python-expert", version="1.0.0"),
    )
    resp = await client.get("/v1/registry?q=typescript")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["name"] == "typescript-helper"


async def test_browse_limit_and_has_more(client: httpx.AsyncClient) -> None:
    """browse respects ``limit=`` and sets ``has_more`` correctly."""
    for i in range(3):
        await client.post("/v1/registry", json=_publish_payload(name=f"agent-{i}"))
    resp = await client.get("/v1/registry?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is True


# ── POST /v1/registry (publish) ───────────────────────────────────────────────


async def test_publish_returns_201(client: httpx.AsyncClient) -> None:
    """``POST /v1/registry`` returns 201 with the created agent."""
    resp = await client.post("/v1/registry", json=_publish_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "my-agent"
    assert body["version"] == "1.0.0"
    assert body["object"] == "published_agent"
    assert body["id"].startswith("pa_")


async def test_publish_with_metadata(client: httpx.AsyncClient) -> None:
    """Published agent round-trips optional metadata fields."""
    payload = _publish_payload(
        category="coding",
        tags=["typescript", "rag"],
        network_access=True,
        guardrails="No shell.",
    )
    resp = await client.post("/v1/registry", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["category"] == "coding"
    assert body["tags"] == ["typescript", "rag"]
    assert body["network_access"] is True
    assert body["guardrails"] == "No shell."


async def test_publish_duplicate_returns_409(client: httpx.AsyncClient) -> None:
    """Publishing the same ``name@version`` twice returns 409."""
    await client.post("/v1/registry", json=_publish_payload())
    resp = await client.post("/v1/registry", json=_publish_payload())
    assert resp.status_code == 409


# ── GET /v1/registry/{name} (latest) ──────────────────────────────────────────


async def test_get_latest_returns_agent(client: httpx.AsyncClient) -> None:
    """``GET /v1/registry/{name}`` returns the agent's latest version."""
    await client.post("/v1/registry", json=_publish_payload())
    resp = await client.get("/v1/registry/my-agent")
    assert resp.status_code == 200
    assert resp.json()["name"] == "my-agent"


async def test_get_latest_returns_404_for_unknown(client: httpx.AsyncClient) -> None:
    """``GET /v1/registry/{name}`` returns 404 when the name is unknown."""
    resp = await client.get("/v1/registry/nonexistent")
    assert resp.status_code == 404


# ── GET /v1/registry/{name}/{version} ─────────────────────────────────────────


async def test_get_version_returns_agent(client: httpx.AsyncClient) -> None:
    """``GET /v1/registry/{name}/{version}`` returns the exact version."""
    await client.post("/v1/registry", json=_publish_payload(version="2.3.1"))
    resp = await client.get("/v1/registry/my-agent/2.3.1")
    assert resp.status_code == 200
    assert resp.json()["version"] == "2.3.1"


async def test_get_version_returns_404_for_wrong_version(
    client: httpx.AsyncClient,
) -> None:
    """``GET /v1/registry/{name}/{version}`` returns 404 for an unpublished version."""
    await client.post("/v1/registry", json=_publish_payload(version="1.0.0"))
    resp = await client.get("/v1/registry/my-agent/9.9.9")
    assert resp.status_code == 404


# ── POST /v1/registry/{name}/star ─────────────────────────────────────────────


async def test_star_increments_and_returns_count(client: httpx.AsyncClient) -> None:
    """``POST /v1/registry/{name}/star`` increments stars and returns the new count."""
    await client.post("/v1/registry", json=_publish_payload())
    resp = await client.post("/v1/registry/my-agent/star")
    assert resp.status_code == 200
    assert resp.json()["stars_count"] == 1

    resp2 = await client.post("/v1/registry/my-agent/star")
    assert resp2.json()["stars_count"] == 2


async def test_star_returns_404_for_unknown(client: httpx.AsyncClient) -> None:
    """``POST /v1/registry/{name}/star`` returns 404 for unknown agents."""
    resp = await client.post("/v1/registry/ghost-agent/star")
    assert resp.status_code == 404
