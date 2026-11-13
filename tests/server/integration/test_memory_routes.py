"""``/v1/memory`` through the real app, auth, sharing and runner binding."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.memory_availability import MEMORY_HEADER, MEMORY_SEARCH_HEADER
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import FEATURES_ENV_VAR, resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.memory_store.hindsight_store import HindsightMemoryStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_permissions import _create_session_as
from tests.stores.fake_hindsight import FakeHindsight

pytestmark = pytest.mark.asyncio

ALICE = {"X-Forwarded-Email": "alice"}
BOB = {"X-Forwarded-Email": "bob"}


@pytest.fixture
def memory_backend() -> str:
    """Backend behind the routes; a test parametrizes ``"hindsight"`` for search."""
    return "sqlalchemy"


@pytest.fixture
def with_permissions() -> bool:
    """Whether the app has a permission store; a test parametrizes ``False``."""
    return True


@pytest.fixture
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_backend: str,
    with_permissions: bool,
) -> FastAPI:
    """An auth-enabled app with a memory store and memory turned on (it is opt-in)."""
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    memory_store = (
        HindsightMemoryStore(FakeHindsight())
        if memory_backend == "hindsight"
        else SqlAlchemyMemoryStore(db_uri)
    )
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri) if with_permissions else None,
        auth_provider=UnifiedAuthProvider(source="header"),
        host_store=HostStore(db_uri),
        memory_store=memory_store,
        feature_flags=resolve_feature_flags({FEATURES_ENV_VAR: "memory"}),
    )


async def _write(client: httpx.AsyncClient, headers: dict[str, str], path: str, content: str):
    return await client.put(
        "/v1/memory/entry", json={"path": path, "content": content}, headers=headers
    )


async def test_owner_writes_reads_lists_and_deletes(auth_client: httpx.AsyncClient):
    written = await auth_client.put(
        "/v1/memory/entry",
        json={
            "path": "/user/preferences/style",
            "content": "Prefers tabs",
            "description": "Style",
        },
        headers=ALICE,
    )
    assert written.status_code == 200, written.text
    assert set(written.json()) == {"path", "content", "description", "created_at", "updated_at"}

    got = await auth_client.get(
        "/v1/memory/entry", params={"path": "/user/preferences/style"}, headers=ALICE
    )
    assert got.json()["content"] == "Prefers tabs"

    listed = await auth_client.get("/v1/memory/entries", params={"prefix": "/user"}, headers=ALICE)
    assert [e["path"] for e in listed.json()["entries"]] == ["/user/preferences/style"]
    assert listed.json()["entries"][0]["content"] == ""

    deleted = await auth_client.delete(
        "/v1/memory/entry", params={"path": "/user/preferences/style"}, headers=ALICE
    )
    assert deleted.json() == {"deleted": True}
    missing = await auth_client.get(
        "/v1/memory/entry", params={"path": "/user/preferences/style"}, headers=ALICE
    )
    assert missing.status_code == 404


async def test_the_longest_user_id_can_use_memory(auth_client: httpx.AsyncClient):
    # 128 characters, the longest user id other tables store; the owner adds "user:".
    longest = {"X-Forwarded-Email": "u" * 121 + "@ex.com"}
    assert len(longest["X-Forwarded-Email"]) == 128

    assert (await _write(auth_client, longest, "/a", "x")).status_code == 200
    listed = await auth_client.get("/v1/memory/entries", headers=longest)
    assert [e["path"] for e in listed.json()["entries"]] == ["/a"]
    assert (await auth_client.get("/v1/memory/entries", headers=ALICE)).json()["entries"] == []


async def test_users_cannot_see_each_others_memory(auth_client: httpx.AsyncClient):
    await _write(auth_client, ALICE, "/secret", "alice only")

    assert (
        await auth_client.get("/v1/memory/entry", params={"path": "/secret"}, headers=BOB)
    ).status_code == 404
    assert (await auth_client.get("/v1/memory/entries", headers=BOB)).json()["entries"] == []
    assert (
        await auth_client.delete("/v1/memory/entry", params={"path": "/secret"}, headers=BOB)
    ).json() == {"deleted": False}


async def test_the_actor_cannot_be_supplied_by_the_request(auth_client: httpx.AsyncClient):
    response = await auth_client.put(
        "/v1/memory/entry",
        json={"path": "/a", "content": "x", "actor_id": "user:alice"},
        headers=BOB,
    )

    assert response.status_code == 422
    assert (
        await auth_client.get("/v1/memory/entry", params={"path": "/a"}, headers=ALICE)
    ).status_code == 404


async def test_a_request_with_no_identity_header_acts_as_the_local_user_not_as_anyone_else(
    auth_client: httpx.AsyncClient,
):
    await _write(auth_client, ALICE, "/secret", "alice only")

    anonymous = await auth_client.get("/v1/memory/entries")

    assert anonymous.status_code == 200
    assert anonymous.json()["entries"] == []


@pytest.mark.parametrize("path", ["relative", "/a/../b", "/"])
async def test_invalid_paths_return_400(auth_client: httpx.AsyncClient, path: str):
    assert (await _write(auth_client, ALICE, path, "x")).status_code == 400


async def test_search_is_not_served_by_a_backend_without_search(auth_client: httpx.AsyncClient):
    await _write(auth_client, ALICE, "/a", "tabs")

    found = await auth_client.post("/v1/memory/search", json={"query": "tabs"}, headers=ALICE)

    assert found.status_code == 404
    assert "not available" in found.text


@pytest.mark.parametrize("memory_backend", ["hindsight"])
async def test_a_backend_that_can_search_serves_only_the_callers_hits(
    auth_client: httpx.AsyncClient,
):
    await _write(auth_client, ALICE, "/user/preferences/style", "Prefers tabs")

    mine = await auth_client.post("/v1/memory/search", json={"query": "tabs"}, headers=ALICE)
    theirs = await auth_client.post("/v1/memory/search", json={"query": "tabs"}, headers=BOB)

    assert [h["entry"]["path"] for h in mine.json()["hits"]] == ["/user/preferences/style"]
    assert theirs.json() == {"hits": []}


@pytest.mark.parametrize("with_permissions", [False])
async def test_without_a_permission_store_tool_calls_are_refused(
    auth_client: httpx.AsyncClient, db_uri: str
):
    session = await _create_session_as(auth_client, "", "alice")
    headers = {
        **ALICE,
        **_bind_runner(db_uri, session["id"]),
        ORCHESTRATION_SOURCE_HEADER: session["id"],
    }

    assert (await _write(auth_client, headers, "/a", "x")).status_code == 403
    # The owner's own API call, with no calling session, is unaffected.
    assert (await _write(auth_client, ALICE, "/a", "x")).status_code == 200


async def test_info_advertises_memory(auth_client: httpx.AsyncClient):
    info = await auth_client.get("/v1/info", headers=ALICE)

    assert info.json()["memory"] is True


async def test_by_default_the_routes_and_the_capability_are_off(
    auth_app: FastAPI, auth_client: httpx.AsyncClient
):
    auth_app.state.feature_flags = resolve_feature_flags({})

    assert (await auth_client.get("/v1/memory/entries", headers=ALICE)).status_code == 404
    assert (await auth_client.get("/v1/info", headers=ALICE)).json()["memory"] is False


async def test_tool_call_from_the_bound_runner_of_a_private_session_is_allowed(
    auth_client: httpx.AsyncClient, db_uri: str
):
    session = await _create_session_as(auth_client, "", "alice")
    headers = {
        **ALICE,
        **_bind_runner(db_uri, session["id"]),
        ORCHESTRATION_SOURCE_HEADER: session["id"],
    }

    assert (await _write(auth_client, headers, "/a", "x")).status_code == 200
    assert (await auth_client.get("/v1/memory/entries", headers=headers)).status_code == 200


async def test_tool_call_without_the_bound_runner_token_is_refused(
    auth_client: httpx.AsyncClient, db_uri: str
):
    session = await _create_session_as(auth_client, "", "alice")
    _bind_runner(db_uri, session["id"])
    headers = {**ALICE, ORCHESTRATION_SOURCE_HEADER: session["id"]}

    assert (await _write(auth_client, headers, "/a", "x")).status_code == 403


async def test_tool_call_naming_another_users_session_is_refused(
    auth_client: httpx.AsyncClient, db_uri: str
):
    bobs = await _create_session_as(auth_client, "", "bob")
    headers = {
        **ALICE,
        **_bind_runner(db_uri, bobs["id"]),
        ORCHESTRATION_SOURCE_HEADER: bobs["id"],
    }

    assert (await _write(auth_client, headers, "/a", "x")).status_code == 403


async def test_tool_call_in_a_session_shared_with_editors_is_refused_but_readers_are_not(
    auth_client: httpx.AsyncClient, db_uri: str
):
    session = await _create_session_as(auth_client, "", "alice")
    headers = {
        **ALICE,
        **_bind_runner(db_uri, session["id"]),
        ORCHESTRATION_SOURCE_HEADER: session["id"],
    }
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")

    permissions.grant("bob", session["id"], LEVEL_READ)
    assert (await _write(auth_client, headers, "/a", "x")).status_code == 200

    permissions.grant("bob", session["id"], LEVEL_EDIT)
    refused = await _write(auth_client, headers, "/b", "y")
    assert refused.status_code == 403
    # Direct use of the API (no calling session) is the owner's own call and still works.
    assert (await _write(auth_client, ALICE, "/c", "z")).status_code == 200


async def _tool(client: httpx.AsyncClient, session_id: str, name: str, **args):
    """Call a memory tool the way the runner does for an agent with memory on."""
    import json

    from omnigent.runner.tool_dispatch import execute_tool
    from omnigent.spec.types import AgentSpec

    return json.loads(
        await execute_tool(
            tool_name=name,
            arguments=json.dumps(args),
            server_client=client,
            agent_spec=AgentSpec(spec_version=1, skills_filter="none"),
            conversation_id=session_id,
        )
    )


async def test_memory_tools_round_trip_through_the_real_server(
    auth_client: httpx.AsyncClient, db_uri: str
):
    session = await _create_session_as(auth_client, "", "alice")
    auth_client.headers.update({**ALICE, **_bind_runner(db_uri, session["id"])})

    written = await _tool(
        auth_client,
        session["id"],
        "memory_write",
        path="/projects/api/retries",
        content="Use exponential backoff",
        description="Retries",
    )
    got = await _tool(auth_client, session["id"], "memory_get", path="/projects/api/retries")
    listed = await _tool(auth_client, session["id"], "memory_list", path_prefix="/projects")
    deleted = await _tool(
        auth_client, session["id"], "memory_delete", path="/projects/api/retries"
    )
    after = await _tool(auth_client, session["id"], "memory_get", path="/projects/api/retries")

    assert written["path"] == "/projects/api/retries"
    assert got["content"] == "Use exponential backoff"
    assert [e["description"] for e in listed["entries"]] == ["Retries"]
    assert deleted == {"deleted": True}
    assert after["error"] == "server returned 404"


async def test_memory_list_pages_through_the_real_server(
    auth_client: httpx.AsyncClient, db_uri: str
):
    session = await _create_session_as(auth_client, "", "alice")
    auth_client.headers.update({**ALICE, **_bind_runner(db_uri, session["id"])})
    paths = [f"/notes/{i:03d}" for i in range(101)]
    for path in paths:
        assert (await _write(auth_client, ALICE, path, "x")).status_code == 200

    first = await _tool(auth_client, session["id"], "memory_list")
    second = await _tool(
        auth_client, session["id"], "memory_list", page_token=first["next_page_token"]
    )

    assert len(first["entries"]) == 100
    assert [e["path"] for e in first["entries"] + second["entries"]] == paths
    assert second["next_page_token"] is None


async def test_memory_tools_are_refused_once_the_session_is_shared_with_an_editor(
    auth_client: httpx.AsyncClient, db_uri: str
):
    session = await _create_session_as(auth_client, "", "alice")
    auth_client.headers.update({**ALICE, **_bind_runner(db_uri, session["id"])})
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", session["id"], LEVEL_EDIT)

    result = await _tool(auth_client, session["id"], "memory_write", path="/a", content="x")

    assert result["error"] == "server returned 403"
    assert (await auth_client.get("/v1/memory/entries", headers={**ALICE})).json()["entries"] == []


async def test_bundle_download_tells_the_runner_whether_memory_is_available(
    auth_app: FastAPI, auth_client: httpx.AsyncClient
):
    session = await _create_session_as(auth_client, "", "alice")
    url = f"/v1/sessions/{session['id']}/agent/contents"

    enabled = await auth_client.get(url, headers=ALICE)
    auth_app.state.feature_flags = resolve_feature_flags({})
    disabled = await auth_client.get(url, headers=ALICE)

    assert enabled.status_code == 200, enabled.text
    assert enabled.headers[MEMORY_HEADER] == "1"
    assert disabled.headers[MEMORY_HEADER] == "0"
    # This backend cannot search, and nothing can while memory is off.
    assert enabled.headers[MEMORY_SEARCH_HEADER] == "0"
    assert disabled.headers[MEMORY_SEARCH_HEADER] == "0"


@pytest.mark.parametrize("memory_backend", ["hindsight"])
async def test_bundle_download_offers_search_for_a_backend_that_can_search(
    auth_app: FastAPI, auth_client: httpx.AsyncClient
):
    session = await _create_session_as(auth_client, "", "alice")
    url = f"/v1/sessions/{session['id']}/agent/contents"

    enabled = await auth_client.get(url, headers=ALICE)
    auth_app.state.feature_flags = resolve_feature_flags({})
    disabled = await auth_client.get(url, headers=ALICE)

    assert enabled.headers[MEMORY_SEARCH_HEADER] == "1"
    assert disabled.headers[MEMORY_SEARCH_HEADER] == "0"
