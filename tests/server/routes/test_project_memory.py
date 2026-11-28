"""``/v1/projects/{id}/memory``: project-owned memory, a shared area and one area per role."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import FEATURES_ENV_VAR, resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.memory_store.hindsight_store import HindsightMemoryStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.routes.test_project_intake import _agent_headers
from tests.server.routes.test_projects_crud import ALICE, BOB, _as_user
from tests.server.routes.test_projects_crud import project_agents as project_agents
from tests.stores.fake_hindsight import FakeHindsight

CAROL = "carol@example.com"


@pytest.fixture(params=["sqlalchemy", "hindsight"])
def memory_app(
    request: pytest.FixtureRequest,
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    project_agents: SqlAlchemyAgentStore,
) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    memory_store = (
        HindsightMemoryStore(FakeHindsight())
        if request.param == "hindsight"
        else SqlAlchemyMemoryStore(db_uri)
    )
    return create_app(
        agent_store=project_agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        memory_store=memory_store,
        feature_flags=resolve_feature_flags({FEATURES_ENV_VAR: "memory"}),
    )


@pytest_asyncio.fixture()
async def client(memory_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=memory_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _session_headers(db_uri: str, session_id: str, user: str) -> dict[str, str]:
    """Headers for a tool call from *session_id* on its bound runner."""
    return {
        **_as_user(user),
        **_bind_runner(db_uri, session_id),
        ORCHESTRATION_SOURCE_HEADER: session_id,
    }


async def test_roles_write_their_own_and_the_shared_area_and_read_everything(client, db_uri):
    project = (
        await client.post("/v1/projects", json={"name": "Mem"}, headers=_as_user(ALICE))
    ).json()
    base = f"/v1/projects/{project['id']}/memory"
    await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=_as_user(ALICE)
    )
    identities = f"/v1/projects/{project['id']}/identities"
    pm = (
        await client.post(
            identities, json={"name": "PM", "instructions": "Plan."}, headers=_as_user(ALICE)
        )
    ).json()
    designer = (
        await client.post(
            identities, json={"name": "Designer", "instructions": "Draw."}, headers=_as_user(ALICE)
        )
    ).json()
    pm_agent = _session_headers(db_uri, pm["active_session_id"], ALICE)
    lead = _session_headers(db_uri, project["main_session_id"], ALICE)

    def put(headers, path, **body):
        return client.put(
            f"{base}/entry", json={"path": path, "content": "x", **body}, headers=headers
        )

    access = (await client.get(base, headers=pm_agent)).json()
    assert access == {
        "project_id": project["id"],
        "identity_id": pm["id"],
        "writable_areas": ["/project", f"/identities/{pm['id']}"],
    }
    assert (await client.get(base, headers=lead)).json()["identity_id"] == "orchestrator"

    # A role writes the shared area and its own; never another role's or outside the layout.
    assert (await put(pm_agent, "/project/stack")).status_code == 200
    assert (await put(pm_agent, f"/identities/{pm['id']}/plan")).status_code == 200
    refused = await put(pm_agent, f"/identities/{designer['id']}/plan")
    assert refused.status_code == 403, refused.text
    assert (await put(pm_agent, "/user/preferences")).status_code == 400
    delete = await client.delete(
        f"{base}/entry", params={"path": "/identities/orchestrator/x"}, headers=pm_agent
    )
    assert delete.status_code == 403
    assert (await put(lead, "/identities/orchestrator/notes")).status_code == 200
    # The body can't name a project or role.
    for extra in ({"identity_id": designer["id"]}, {"project_id": project["id"]}):
        assert (await put(pm_agent, f"/identities/{designer['id']}/x", **extra)).status_code == 422

    # People on the project write anywhere in the layout and see the same memory.
    assert (await put(_as_user(BOB), f"/identities/{designer['id']}/brief")).status_code == 200
    listed = (await client.get(f"{base}/entries", headers=lead)).json()
    assert sorted(entry["path"] for entry in listed["entries"]) == sorted(
        [
            f"/identities/{designer['id']}/brief",
            "/identities/orchestrator/notes",
            f"/identities/{pm['id']}/plan",
            "/project/stack",
        ]
    )
    read = await client.get(f"{base}/entry", params={"path": "/project/stack"}, headers=pm_agent)
    assert read.status_code == 200 and read.json()["content"] == "x"

    # A worker session in the project reads but holds no role, so it can't write.
    worker = _agent_headers(db_uri, project["id"], ALICE)
    assert (await client.get(f"{base}/entries", headers=worker)).status_code == 200
    assert (await put(worker, "/project/stack")).status_code == 403
    # Strangers, sessions of another project and unbound runners are refused.
    assert (await client.get(f"{base}/entries", headers=_as_user(CAROL))).status_code == 404
    other = (
        await client.post("/v1/projects", json={"name": "Other"}, headers=_as_user(ALICE))
    ).json()
    outsider = _session_headers(db_uri, other["main_session_id"], ALICE)
    assert (await client.get(f"{base}/entries", headers=outsider)).status_code == 404
    unbound = {**_as_user(ALICE), ORCHESTRATION_SOURCE_HEADER: pm["active_session_id"]}
    assert (await put(unbound, "/project/stack")).status_code == 403

    # Personal memory stays apart: the owner's own /v1/memory doesn't see project entries.
    mine = (await client.get("/v1/memory/entries", headers=_as_user(ALICE))).json()
    assert mine["entries"] == []
