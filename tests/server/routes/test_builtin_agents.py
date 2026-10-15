"""Tests for the builtin agents discovery route (``GET /v1/agents``).

The app fixture does not trigger the lifespan event that seeds
built-in agents, so the test database starts empty. We seed a
test agent directly via the agent_store to verify the endpoint works.
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from omnigent.db.utils import builtin_agent_id, generate_agent_id
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


async def test_builtin_flag_distinguishes_seeded_from_registered(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``builtin`` is True only for a server-seeded agent (deterministic,
    name-derived id); an operator/user-registered template (random id)
    reports False. The Web UI picker keys its supersession policy on this:
    a same-named upload may shadow a registered template but never a seeded
    built-in.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    seeded_id = builtin_agent_id("polly")
    agent_store.create(seeded_id, name="polly", bundle_location="test:///polly")
    registered_id = generate_agent_id()
    agent_store.create(registered_id, name="my-agent", bundle_location="test:///mine")

    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    by_id = {a["id"]: a for a in resp.json()["data"]}
    assert by_id[seeded_id]["builtin"] is True
    assert by_id[registered_id]["builtin"] is False


async def test_suppressed_agents_are_hidden_from_discovery(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A packaged built-in the deployment suppressed is omitted from the list.

    This is what trims the picker on a deployment whose database was seeded
    before ``OMNIGENT_SEEDED_AGENTS`` was set: the row is still there (and
    still bound to any session that used it), but discovery skips it. An
    operator's own agent is never suppressed, so it keeps its place.
    """
    from omnigent.server import seeded_agents

    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(builtin_agent_id("polly"), name="polly", bundle_location="test:///p")
    agent_store.create(
        builtin_agent_id("house-agent"), name="house-agent", bundle_location="test:///h"
    )

    seeded_agents.record_suppressed_agents(frozenset({"polly"}))
    try:
        resp = await client.get("/v1/agents?limit=100")
    finally:
        seeded_agents.record_suppressed_agents(frozenset())

    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()["data"]]
    assert "polly" not in names
    assert "house-agent" in names
    # Hidden, not deleted — the row (and its cascade-linked history) survives.
    assert agent_store.get_by_name("polly") is not None


async def test_list_publishes_suppressed_agent_names(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    The list names the packaged built-ins it hides, so a client that also
    discovers agents from session history (the web picker) can drop a hidden
    built-in instead of resurfacing it as a custom agent.
    """
    from omnigent.server import seeded_agents

    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(builtin_agent_id("polly"), name="polly", bundle_location="test:///p")
    seeded_agents.record_suppressed_agents(frozenset({"polly", "debby"}))
    try:
        resp = await client.get("/v1/agents?limit=100")
    finally:
        seeded_agents.record_suppressed_agents(frozenset())

    assert resp.status_code == 200
    body = resp.json()
    assert body["suppressed_agent_names"] == ["debby", "polly"]
    assert "polly" not in [a["name"] for a in body["data"]]


async def test_list_publishes_no_suppressed_names_by_default(
    client: httpx.AsyncClient,
) -> None:
    """An untrimmed deployment reports an empty list, never a missing field."""
    resp = await client.get("/v1/agents?limit=5")
    assert resp.status_code == 200
    assert resp.json()["suppressed_agent_names"] == []
