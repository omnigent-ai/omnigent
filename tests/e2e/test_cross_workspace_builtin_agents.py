"""Built-in agents must be resolvable from every workspace — without this,
built-ins are invisible outside the default workspace and a session cannot
be started in a second workspace at all.

The server lifespan seeds built-in agents via ``_ensure_default_agents``
with **no workspace bound**, so the rows land in
``DEFAULT_WORKSPACE_ID`` (0) only. Every ``agent_store`` read filters by
``current_workspace_id()``, so on a multi-tenant deployment that binds a
per-request workspace (``workspace_scope`` in middleware), any workspace
other than 0 cannot resolve the built-ins:

* the new-session picker (``GET /v1/agents``) lists no built-in agents;
* ``POST /v1/sessions`` binding the built-in's deterministic id 404s
  ("Agent not found"), so no session can be started at all;
* the runner spec resolver (``GET /v1/sessions/{id}/agent/contents``)
  404-loops on ``agent 58a1bc5bf0bba6d31ceeb7661f8d751c for session ...
  was not found`` (``builtin_agent_id("claude-native-ui") ==
  "58a1bc5bf0bba6d31ceeb7661f8d751c"``).

These tests drive the real FastAPI app over HTTP with the built-ins
seeded exactly the way the lifespan seeds them, then bind a non-default
workspace around each request the way the multi-tenant middleware does.
They fail whenever built-in agents stop being resolvable from a
non-default workspace.

Runs entirely in-process — no real LLM, runner, or network needed::

    pytest tests/e2e/test_cross_workspace_builtin_agents.py -v
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import builtin_agent_id
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import _ensure_default_agents, create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

pytestmark = pytest.mark.asyncio

# The workspace id the multi-tenant middleware would bind for a second
# workspace on the same regional server. Any non-zero id shows the bug.
_SECOND_WORKSPACE_ID = 2

# The default built-in agent the runner spec resolver 404-loops on:
# builtin_agent_id("claude-native-ui") == "58a1bc5bf0bba6d31ceeb7661f8d751c".
_BUILTIN_NAME = "claude-native-ui"


@pytest.fixture()
def app(db_uri: str, tmp_path: Path) -> FastAPI:
    """FastAPI app with real stores and lifespan-style built-in seeding.

    ``_ensure_default_agents`` is called exactly the way the server
    lifespan calls it — with NO workspace bound — so the built-in rows
    land wherever the production seeding puts them. The test must not
    pre-scope the seeding: that context-free call is the mechanism under
    test.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_cache = AgentCache(
        artifact_store=artifact_store,
        cache_dir=tmp_path / "cache",
    )
    _ensure_default_agents(agent_store, artifact_store, agent_cache)
    return create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=agent_cache,
    )


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> httpx.AsyncClient:
    """HTTP client wired to the app in-process.

    ``httpx.ASGITransport`` awaits the app in the caller's context, so a
    ``workspace_scope`` entered around a request propagates into the
    route handler exactly like the multi-tenant middleware's binding
    (including through ``asyncio.to_thread``, which copies the context).
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _agent_ids(payload: dict) -> set[str]:
    """Extract the agent ids from a ``GET /v1/agents`` response body."""
    return {str(item["id"]) for item in payload.get("data", [])}


async def test_builtin_agents_listed_in_second_workspace(
    client: httpx.AsyncClient,
) -> None:
    """The new-session picker must offer built-ins in every workspace.

    Control first: the default workspace sees the seeded built-in, so a
    failure below is the cross-workspace bug, not a seeding problem.
    Under a non-default workspace scope, ``GET /v1/agents`` must not
    return an empty catalog — an empty picker in the second workspace
    has nothing to bind, which is the first user-visible step of the
    "cannot start a session" journey.
    """
    agent_id = builtin_agent_id(_BUILTIN_NAME)

    resp = await client.get("/v1/agents", params={"limit": 100})
    assert resp.status_code == 200
    assert agent_id in _agent_ids(resp.json()), (
        "control failed: built-in agent missing even in the default workspace"
    )

    with workspace_scope(_SECOND_WORKSPACE_ID):
        resp = await client.get("/v1/agents", params={"limit": 100})
        assert resp.status_code == 200
        assert agent_id in _agent_ids(resp.json()), (
            f"built-in agent {_BUILTIN_NAME!r} ({agent_id}) is invisible in "
            f"workspace {_SECOND_WORKSPACE_ID}: built-ins were seeded into the "
            "default workspace only, so a second workspace has no agents to offer"
        )


async def test_session_with_builtin_agent_starts_in_second_workspace(
    client: httpx.AsyncClient,
) -> None:
    """A session bound to a built-in agent must start in every workspace.

    Drives the failing user journey inside a second workspace: create a
    session with the default built-in agent, then resolve the agent
    bundle the way the runner's spec resolver does
    (``GET /v1/sessions/{id}/agent/contents`` — the endpoint that
    404-loops when the bug is live). Unfixed, the create itself 404s
    with "Agent not found: '58a1bc...'" because the built-in row only
    exists in workspace 0.
    """
    agent_id = builtin_agent_id(_BUILTIN_NAME)
    headers = {"Origin": OMNIGENT_INTERNAL_WS_ORIGIN}

    # Control: the same journey succeeds in the default workspace.
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id}, headers=headers)
    assert resp.status_code == 201, (
        f"control failed: session create in the default workspace returned "
        f"{resp.status_code}: {resp.text}"
    )

    with workspace_scope(_SECOND_WORKSPACE_ID):
        resp = await client.post("/v1/sessions", json={"agent_id": agent_id}, headers=headers)
        assert resp.status_code == 201, (
            f"cannot start a session in workspace {_SECOND_WORKSPACE_ID} with the "
            f"built-in agent {_BUILTIN_NAME!r} ({agent_id}): "
            f"{resp.status_code}: {resp.text}"
        )
        session_id = str(resp.json()["id"])

        # The runner's spec resolve — the endpoint that 404-loops when the
        # bug is live — must serve the built-in bundle from this workspace too.
        resp = await client.get(f"/v1/sessions/{session_id}/agent/contents")
        assert resp.status_code == 200, (
            f"runner spec resolver GET /v1/sessions/{session_id}/agent/contents "
            f"failed in workspace {_SECOND_WORKSPACE_ID}: "
            f"{resp.status_code}: {resp.text}"
        )
