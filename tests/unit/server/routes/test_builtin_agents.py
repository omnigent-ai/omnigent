"""Tests for the builtin agents discovery route (``GET /v1/agents``).

The app fixture does not trigger the lifespan event that seeds
built-in agents, so the test database starts empty. We seed a
test agent directly via the agent_store to verify the endpoint works.
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


@pytest_asyncio.fixture()
async def _seeded_agent(db_uri: str) -> str:
    """Seed a built-in (session_id=None) agent and return its ID."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-builtin", bundle_location="test:///bundle")
    return agent_id


async def test_list_builtin_agents_empty(client: httpx.AsyncClient) -> None:
    """GET /v1/agents with no agents returns an empty paginated list."""
    resp = await client.get("/v1/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["data"], list)
    assert "has_more" in body


async def test_list_builtin_agents_with_limit(client: httpx.AsyncClient) -> None:
    """Limit parameter constrains the result size."""
    resp = await client.get("/v1/agents?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) <= 1


async def test_list_builtin_agents_seeded(
    client: httpx.AsyncClient,
    _seeded_agent: str,
) -> None:
    """A seeded agent appears in the list."""
    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()["data"]]
    assert _seeded_agent in ids


async def test_list_builtin_agents_response_shape(
    client: httpx.AsyncClient,
    _seeded_agent: str,
) -> None:
    """Each agent object has the expected fields."""
    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    for agent in resp.json()["data"]:
        assert "id" in agent
        assert "name" in agent
        assert "created_at" in agent
