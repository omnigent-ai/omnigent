"""User agents: ``POST`` / ``GET ?scope=user`` / ``DELETE /v1/agents`` and visibility.

A multi-user server must never let one user overwrite, see, remove, or bind
another user's agent, nor touch a server agent. These run against a strict
header-auth provider (no single-user ``"local"`` fallback), the posture of a
deployed server.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import generate_agent_id, installed_agent_id
from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes._session_create_validation import require_user_agent_visible
from omnigent.server.routes.builtin_agents import create_builtin_agents_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import build_agent_bundle

ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}


@pytest.fixture()
def agent_store(db_uri: str) -> SqlAlchemyAgentStore:
    return SqlAlchemyAgentStore(db_uri)


@pytest.fixture()
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(str(tmp_path / "artifacts"))


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


def _app(
    agent_store, artifact_store, tmp_path: Path, auth_provider, conversation_store=None
) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.include_router(
        create_builtin_agents_router(
            agent_store,
            AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
            artifact_store=artifact_store,
            conversation_store=conversation_store,
            auth_provider=auth_provider,
        ),
        prefix="/v1",
    )
    return app


@pytest_asyncio.fixture()
async def client(
    agent_store, artifact_store, conversation_store, tmp_path
) -> AsyncIterator[httpx.AsyncClient]:
    """Client for a multi-user server: identity from ``X-Forwarded-Email``."""
    provider = UnifiedAuthProvider(source="header", local_single_user=False)
    app = _app(agent_store, artifact_store, tmp_path, provider, conversation_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture()
async def app_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Client for the full server app (the conftest ``app`` fixture)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _install(
    client: httpx.AsyncClient, headers: dict[str, str], name: str, description: str = "v1"
) -> httpx.Response:
    bundle = build_agent_bundle(name, description=description)
    return await client.post(
        "/v1/agents",
        headers=headers,
        files={"bundle": ("bundle.tar.gz", bundle, "application/gzip")},
    )


async def _mine(client: httpx.AsyncClient, headers: dict[str, str], **params: str) -> dict:
    resp = await client.get("/v1/agents", params={"scope": "user", **params}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _my_names(client: httpx.AsyncClient, headers: dict[str, str]) -> list[str]:
    return [row["name"] for row in (await _mine(client, headers))["data"]]


async def test_install_creates_a_user_agent_only_its_owner_lists(client, agent_store) -> None:
    resp = await _install(client, ALICE, "orion")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["builtin"] is False
    assert body["id"] == installed_agent_id("alice@example.com", "orion")
    assert agent_store.get(body["id"]).created_by == "alice@example.com"

    assert await _my_names(client, ALICE) == ["orion"]
    assert await _my_names(client, BOB) == []
    # The default listing is server agents only, exactly as before.
    default = await client.get("/v1/agents", headers=ALICE)
    assert default.status_code == 200 and default.json()["data"] == []


async def test_reinstall_updates_in_place(client) -> None:
    first = (await _install(client, ALICE, "orion", "v1")).json()
    second = (await _install(client, ALICE, "orion", "v2")).json()
    assert second["id"] == first["id"], "reinstall must keep the agent id stable"
    assert second["version"] == first["version"] + 1
    assert second["description"] == "v2", "reinstall must show the new bundle's description"
    assert [row["description"] for row in (await _mine(client, ALICE))["data"]] == ["v2"]


async def test_identical_reinstall_is_a_no_op(client) -> None:
    first = (await _install(client, ALICE, "orion", "same")).json()
    again = (await _install(client, ALICE, "orion", "same")).json()
    assert (again["id"], again["version"]) == (first["id"], first["version"])


async def test_same_name_for_another_user_is_a_separate_agent(client, agent_store) -> None:
    alice = (await _install(client, ALICE, "orion", "alice")).json()
    bob = (await _install(client, BOB, "orion", "bob")).json()
    assert bob["id"] != alice["id"]
    assert agent_store.get(alice["id"]).version == 1, "Bob's install touched Alice's agent"


async def test_server_agent_names_are_reserved(client, agent_store) -> None:
    """A same-named user agent would hide behind the server agent in the picker."""
    agent_store.create(generate_agent_id(), "polly", "test:///polly")
    resp = await _install(client, ALICE, "polly")
    assert resp.status_code == 409, resp.text
    assert "server agent" in resp.json()["error"]
    assert await _my_names(client, ALICE) == []


async def test_oversized_bundle_is_413(client, monkeypatch) -> None:
    monkeypatch.setattr("omnigent.server.routes.builtin_agents.MAX_INSTALL_BUNDLE_BYTES", 10)
    resp = await _install(client, ALICE, "orion")
    assert resp.status_code == 413, resp.text


async def test_oversized_streamed_bundle_is_rejected_before_it_is_all_read(
    client, monkeypatch
) -> None:
    """A chunked upload with no Content-Length is cut off once it passes the cap."""
    monkeypatch.setattr("omnigent.server.routes.builtin_agents.MAX_INSTALL_BUNDLE_BYTES", 1024)
    monkeypatch.setattr("omnigent.server.routes.builtin_agents._MULTIPART_OVERHEAD_BYTES", 0)
    sent = 0

    async def body():
        nonlocal sent
        yield (
            b'--b\r\nContent-Disposition: form-data; name="bundle"; filename="b.tar.gz"\r\n'
            b"Content-Type: application/gzip\r\n\r\n"
        )
        for _ in range(200):
            sent += 1
            yield b"x" * 512

    resp = await client.post(
        "/v1/agents",
        headers={**ALICE, "content-type": "multipart/form-data; boundary=b"},
        content=body(),
    )
    assert resp.status_code == 413, resp.text
    assert sent < 200, "the server kept reading past the cap"


async def test_invalid_bundle_is_rejected_without_a_row(client, agent_store) -> None:
    resp = await client.post(
        "/v1/agents",
        headers=ALICE,
        files={"bundle": ("bundle.tar.gz", b"not a tarball", "application/gzip")},
    )
    assert resp.status_code == 400, resp.text
    assert await _my_names(client, ALICE) == []


async def test_install_without_bundle_is_422(client) -> None:
    resp = await client.post("/v1/agents", headers=ALICE, files={"other": ("x", b"x")})
    assert resp.status_code == 422


async def test_local_server_refuses_an_install_from_a_foreign_origin(
    agent_store, artifact_store, tmp_path, monkeypatch
) -> None:
    """A multipart post skips the CORS preflight, so a foreign page must not install."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    app = _app(agent_store, artifact_store, tmp_path, auth_provider=None)
    files = {"bundle": ("bundle.tar.gz", build_agent_bundle("orion"), "application/gzip")}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        foreign = await c.post("/v1/agents", headers={"Origin": "https://evil.test"}, files=files)
        assert foreign.status_code == 403, foreign.text
        assert agent_store.get(installed_agent_id(None, "orion")) is None
        local = await c.post(
            "/v1/agents", headers={"Origin": "http://127.0.0.1:6767"}, files=files
        )
        assert local.status_code == 200, local.text


async def test_concurrent_install_updates_the_winner(client, agent_store, monkeypatch) -> None:
    """The losing side of a get-then-create race updates the winner's row."""
    winner = (await _install(client, ALICE, "orion", "first")).json()
    real_get = agent_store.get
    calls = []

    def stale_then_real(agent_id):
        calls.append(agent_id)
        return None if len(calls) == 1 else real_get(agent_id)

    # The first lookup misses as if a concurrent install had not committed yet.
    monkeypatch.setattr(agent_store, "get", stale_then_real)
    resp = await _install(client, ALICE, "orion", "second")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == winner["id"]
    assert resp.json()["description"] == "second"


async def test_my_agents_page_with_after(client) -> None:
    for name in ("a", "b", "c"):
        assert (await _install(client, ALICE, name)).status_code == 200

    first = await _mine(client, ALICE, limit="2")
    assert len(first["data"]) == 2 and first["has_more"] is True
    rest = await _mine(client, ALICE, limit="2", after=first["last_id"])
    assert len(rest["data"]) == 1 and rest["has_more"] is False
    names = [row["name"] for row in first["data"] + rest["data"]]
    assert sorted(names) == ["a", "b", "c"]


@pytest.mark.parametrize("params", [{"before": "x"}, {"order": "asc"}])
async def test_my_agents_page_forward_only(client, params) -> None:
    resp = await client.get("/v1/agents", params={"scope": "user", **params}, headers=ALICE)
    assert resp.status_code == 400, resp.text


async def test_my_agents_stale_cursor_is_400(client) -> None:
    resp = await client.get(
        "/v1/agents", params={"scope": "user", "after": generate_agent_id()}, headers=ALICE
    )
    assert resp.status_code == 400, resp.text


async def test_remove_is_owner_only(client, agent_store) -> None:
    mine = (await _install(client, ALICE, "orion")).json()
    server_agent_id = generate_agent_id()
    agent_store.create(server_agent_id, "shared", "test:///shared")
    unowned = agent_store.create_user_agent(
        generate_agent_id(), "legacy", "test:///legacy", owner=None
    )

    bobs_attempt = await client.delete(f"/v1/agents/{mine['id']}", headers=BOB)
    assert bobs_attempt.status_code == 404
    for other in (server_agent_id, unowned.id):
        attempt = await client.delete(f"/v1/agents/{other}", headers=ALICE)
        assert attempt.status_code == 404
        assert agent_store.get(other) is not None

    resp = await client.delete(f"/v1/agents/{mine['id']}", headers=ALICE)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": mine["id"], "deleted": True}
    assert agent_store.get(mine["id"]) is None


async def test_remove_warns_while_sessions_use_the_agent(client, conversation_store) -> None:
    """Removing breaks the sessions using it, so it needs force=true."""
    mine = (await _install(client, ALICE, "orion")).json()
    conversation_store.create_conversation(agent_id=mine["id"], title="uses orion")

    resp = await client.delete(f"/v1/agents/{mine['id']}", headers=ALICE)
    assert resp.status_code == 409, resp.text
    assert resp.json()["sessions_in_use"] == "1"
    assert resp.json()["error"]["code"] == "agent_in_use"

    forced = await client.delete(f"/v1/agents/{mine['id']}?force=true", headers=ALICE)
    assert forced.status_code == 200, forced.text
    assert await _my_names(client, ALICE) == []


async def test_remove_counts_sessions_in_use_up_to_a_cap(
    client, conversation_store, monkeypatch
) -> None:
    monkeypatch.setattr("omnigent.server.routes.builtin_agents._IN_USE_COUNT_CAP", 2)
    mine = (await _install(client, ALICE, "orion")).json()
    for _ in range(4):
        conversation_store.create_conversation(agent_id=mine["id"], title="uses orion")

    assert conversation_store.count_sessions_for_agent(mine["id"], 3) == 3
    resp = await client.delete(f"/v1/agents/{mine['id']}", headers=ALICE)
    assert resp.status_code == 409, resp.text
    assert resp.json()["sessions_in_use"] == "2+"


async def test_store_without_user_agents_hides_the_routes(client, monkeypatch) -> None:
    monkeypatch.setattr(SqlAlchemyAgentStore, "supports_user_agents", property(lambda self: False))
    assert (await _install(client, ALICE, "orion")).status_code == 404
    listing = await client.get("/v1/agents", params={"scope": "user"}, headers=ALICE)
    assert listing.status_code == 404
    removal = await client.delete(f"/v1/agents/{generate_agent_id()}", headers=ALICE)
    assert removal.status_code == 404
    assert (await client.get("/v1/agents", headers=ALICE)).status_code == 200


def test_unused_user_agents_bind_only_for_their_owner() -> None:
    """A user agent no session uses binds for its owner only (never when unowned);
    server agents and agents a session uses pass through."""
    base = {"created_at": 0, "name": "n", "bundle_location": "x"}
    owned = Agent(id="ag1", **base, kind="user", created_by="alice@example.com")
    require_user_agent_visible(owned, "alice@example.com")
    for agent, caller in ((owned, "bob@example.com"), (Agent(id="ag4", **base, kind="user"), "b")):
        with pytest.raises(OmnigentError):
            require_user_agent_visible(agent, caller)
    require_user_agent_visible(Agent(id="ag2", **base), "bob")
    used = Agent(id="ag3", **base, kind="user", session_id="conv", created_by="a")
    require_user_agent_visible(used, "b")


def test_only_server_agents_expand_server_env() -> None:
    """User agents are tenant input: no ``${VAR}`` expansion, used or not."""
    base = {"id": "ag", "created_at": 0, "name": "n", "bundle_location": "x"}
    assert Agent(**base).operator_authored
    assert not Agent(**base, created_by="alice@example.com").operator_authored
    assert not Agent(**base, session_id="conv_1").operator_authored
    assert not Agent(**base, session_id="conv_1", created_by="alice@example.com").operator_authored
    # A user agent whose sessions were all deleted still never expands.
    assert not Agent(**base, kind="user").operator_authored


async def test_session_of_removed_agent_says_how_to_continue(
    app_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A session using a since-removed agent tells the user to fork."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id=generate_agent_id(), title="orphaned"
    )
    resp = await app_client.get(f"/v1/sessions/{conv.id}/agent")
    contents = await app_client.get(f"/v1/sessions/{conv.id}/agent/contents")
    assert contents.status_code == 404, contents.text
    # 404, not 410: the runner's spec resolver treats only 404 as "agent missing".
    assert resp.status_code == 404, resp.text
    assert "Fork this session into another agent to continue" in resp.text


@pytest_asyncio.fixture()
async def multi_user_client(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    """Full server app on a multi-user server (identity from ``X-Forwarded-Email``)."""
    from omnigent.server.app import create_app
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_new_session_from_another_users_agent_runs_on_your_copy(
    multi_user_client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """Bob may start from Alice's agent (he can read a session using it), but on his
    own copy, so Alice reinstalling it never changes code in Bob's sessions."""
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    c = multi_user_client
    agents = SqlAlchemyAgentStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    orion = (await _install(c, ALICE, "orion", "v1")).json()

    # Nobody else can bind an agent no session uses.
    denied = await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=BOB)
    assert denied.status_code == 404, denied.text

    alices = await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)
    assert alices.status_code == 201, alices.text
    assert alices.json()["agent_id"] == orion["id"], "the owner binds the agent itself"
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("bob@example.com")
    perms.grant("bob@example.com", alices.json()["id"], 1)

    bobs = await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=BOB)
    assert bobs.status_code == 201, bobs.text
    copy = agents.get(bobs.json()["agent_id"])
    assert copy is not None and copy.id != orion["id"]
    assert (copy.name, copy.created_by) == ("orion", "bob@example.com")
    assert copy.bundle_location.startswith(f"{copy.id}/"), "the copy owns its blob"
    orion_location = agents.get(orion["id"]).bundle_location
    assert artifacts.get(copy.bundle_location) == artifacts.get(orion_location)
    assert [row["id"] for row in (await _mine(c, BOB))["data"]] == [copy.id]

    await _install(c, ALICE, "orion", "v2")
    assert agents.get(orion["id"]).bundle_location != orion_location
    assert agents.get(copy.id).bundle_location == copy.bundle_location


async def test_a_same_named_upload_and_install_stay_separately_manageable(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Names may repeat, so every agent is listed and removed by its own id."""
    from tests.server.helpers import create_test_agent

    c = multi_user_client
    installed = (await _install(c, ALICE, "orion", "v1")).json()
    uploaded = await create_test_agent(c, name="orion", user="alice@example.com")
    assert uploaded["id"] != installed["id"]

    reinstalled = (await _install(c, ALICE, "orion", "v2")).json()
    assert (reinstalled["id"], reinstalled["version"]) == (installed["id"], 2)
    listed = {row["id"]: row["name"] for row in (await _mine(c, ALICE))["data"]}
    assert listed == {installed["id"]: "orion", uploaded["id"]: "orion"}

    removed = await c.delete(f"/v1/agents/{uploaded['id']}?force=true", headers=ALICE)
    assert removed.status_code == 200, removed.text
    assert [row["id"] for row in (await _mine(c, ALICE))["data"]] == [installed["id"]]


async def test_children_and_schedules_run_another_users_agent_on_your_copy(
    multi_user_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A parent link or a schedule never runs another user's agent uncopied; only a
    child reusing its parent's agent stays on it (a collaborator in that session)."""
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    c = multi_user_client
    agents = SqlAlchemyAgentStore(db_uri)
    orion = (await _install(c, ALICE, "orion")).json()
    alices = (await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)).json()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("bob@example.com")
    perms.grant("bob@example.com", alices["id"], 1)
    vega = (await _install(c, BOB, "vega")).json()
    bobs = (await c.post("/v1/sessions", json={"agent_id": vega["id"]}, headers=BOB)).json()

    def bound_owner(resp: httpx.Response) -> str | None:
        assert resp.status_code in (200, 201), resp.text
        agent = agents.get(resp.json()["agent_id"])
        assert agent is not None
        return agent.created_by

    under_bobs_root = await c.post(
        "/v1/sessions",
        json={"agent_id": orion["id"], "parent_session_id": bobs["id"]},
        headers=BOB,
    )
    assert bound_owner(under_bobs_root) == "bob@example.com"
    in_alices_tree = await c.post(
        "/v1/sessions",
        json={"agent_id": orion["id"], "parent_session_id": alices["id"]},
        headers=BOB,
    )
    assert in_alices_tree.json()["agent_id"] == orion["id"], in_alices_tree.text
    schedule = await c.post(
        "/v1/scheduled-tasks",
        json={"name": "nightly", "prompt": "go", "rrule": "FREQ=DAILY", "agent_id": orion["id"]},
        headers=BOB,
    )
    assert bound_owner(schedule) == "bob@example.com"


async def test_a_failed_schedule_switch_leaves_no_agent_copy(
    multi_user_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching a task onto another user's agent copies it first, so a task save
    that then fails must not leave the copy in the caller's agents."""
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    c = multi_user_client
    orion = (await _install(c, ALICE, "orion")).json()
    alices = (await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)).json()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("bob@example.com")
    perms.grant("bob@example.com", alices["id"], 1)
    vega = (await _install(c, BOB, "vega")).json()
    task = await c.post(
        "/v1/scheduled-tasks",
        json={"name": "nightly", "prompt": "go", "rrule": "FREQ=DAILY", "agent_id": vega["id"]},
        headers=BOB,
    )
    assert task.status_code in (200, 201), task.text

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("task store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(SqlAlchemyScheduledTaskStore, "update", fail)
        with pytest.raises(RuntimeError):
            await c.patch(
                f"/v1/scheduled-tasks/{task.json()['id']}",
                json={"agent_id": orion["id"]},
                headers=BOB,
            )

    assert [row["id"] for row in (await _mine(c, BOB))["data"]] == [vega["id"]]
    saved = SqlAlchemyScheduledTaskStore(db_uri).get(task.json()["id"])
    assert saved is not None and saved.agent_id == vega["id"]


async def test_mcp_edits_change_the_agent_for_every_session_using_it(
    multi_user_client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """No copy-on-write: one agent row, so an owner's MCP edit reaches all its
    sessions. Server agents stay read-only through the same endpoint."""
    c = multi_user_client
    orion = (await _install(c, ALICE, "orion")).json()
    first = (await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)).json()
    second = (await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)).json()
    server = {"name": "docs", "transport": "http", "url": "https://example.invalid/mcp"}

    added = await c.post(
        f"/v1/sessions/{first['id']}/agent/mcp-servers", json=server, headers=ALICE
    )
    assert added.status_code == 200, added.text
    listed = await c.get(f"/v1/sessions/{second['id']}/agent/mcp-servers", headers=ALICE)
    assert [s["name"] for s in listed.json()["data"]] == ["docs"]
    assert SqlAlchemyAgentStore(db_uri).get(orion["id"]).version == 2

    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    server_agent_id = generate_agent_id()
    bundle = build_agent_bundle("shared-agent")
    location = f"{server_agent_id}/{'0' * 64}"
    artifacts.put(location, bundle)
    SqlAlchemyAgentStore(db_uri).create(server_agent_id, "shared-agent", location)
    third = await c.post("/v1/sessions", json={"agent_id": server_agent_id}, headers=ALICE)
    assert third.status_code == 201, third.text
    refused = await c.post(
        f"/v1/sessions/{third.json()['id']}/agent/mcp-servers", json=server, headers=ALICE
    )
    assert refused.status_code == 400, refused.text
    assert "read-only" in refused.text


async def test_reinstall_reaches_every_session_on_its_next_turn(
    multi_user_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reinstall keeps the id, so each turn names the bundle revision (a
    create's initial items too); every session's runner sees the new one on its
    next turn and rebuilds the spec."""
    c = multi_user_client
    forwarded: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            session_id = request.url.path.split("/")[3]
            forwarded.setdefault(session_id, []).append(
                json.loads(request.content)["agent_revision"]
            )
        return httpx.Response(202, json={"queued": True})

    async def to_fake_runner(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return runner

    async def relay_ready(*_args: object, **_kwargs: object) -> None:
        return None

    text = {"type": "input_text", "text": "hi"}
    message = {"type": "message", "data": {"role": "user", "content": [text]}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as runner:
        monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", to_fake_runner)
        monkeypatch.setattr(
            "omnigent.server.routes.sessions._ensure_runner_relay_ready", relay_ready
        )
        orion = (await _install(c, ALICE, "orion", "v1")).json()
        # One session starts from its initial items, the other from a first message.
        kickoff = {"agent_id": orion["id"], "initial_items": [message]}
        sessions = [
            (await c.post("/v1/sessions", json=kickoff, headers=ALICE)).json(),
            (await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)).json(),
        ]
        sent = await c.post(
            f"/v1/sessions/{sessions[1]['id']}/events", json=message, headers=ALICE
        )
        assert sent.status_code == 202, sent.text
        await _install(c, ALICE, "orion", "v2")
        for session in sessions:
            sent = await c.post(
                f"/v1/sessions/{session['id']}/events", json=message, headers=ALICE
            )
            assert sent.status_code == 202, sent.text

    v2 = SqlAlchemyAgentStore(db_uri).get(orion["id"]).bundle_location
    for session in sessions:
        before, after = forwarded[session["id"]]
        assert before != after == v2


async def test_a_user_agent_stays_private_after_its_last_session_is_deleted(
    multi_user_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Deleting sessions never deletes the agent, and a retained agent binds only
    for its owner; an unowned one binds for no one."""
    c = multi_user_client
    orion = (await _install(c, ALICE, "orion")).json()
    session = await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)
    assert session.status_code == 201, session.text
    deleted = await c.delete(f"/v1/sessions/{session.json()['id']}", headers=ALICE)
    assert deleted.status_code in (200, 204), deleted.text
    assert SqlAlchemyAgentStore(db_uri).get(orion["id"]) is not None

    bobs = await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=BOB)
    assert bobs.status_code == 404, bobs.text
    alices = await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)
    assert alices.status_code == 201, alices.text

    legacy = SqlAlchemyConversationStore(db_uri).create_session_with_agent(
        agent_id=generate_agent_id(),
        agent_name="legacy",
        agent_bundle_location="x/legacy",
        agent_description=None,
        title="pre-ownership session",
    )
    await SqlAlchemyConversationStore(db_uri).delete_conversation(legacy.conversation.id)
    for headers in (ALICE, BOB):
        attempt = await c.post("/v1/sessions", json={"agent_id": legacy.agent.id}, headers=headers)
        assert attempt.status_code == 404, attempt.text


async def test_runner_never_expands_an_installed_agents_variables(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """The runner-facing provenance header marks user agents as tenant input."""
    c = multi_user_client
    orion = (await _install(c, ALICE, "orion")).json()
    session = (await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)).json()

    contents = await c.get(f"/v1/sessions/{session['id']}/agent/contents", headers=ALICE)

    assert contents.status_code == 200, contents.text
    assert contents.headers["X-Agent-Session-Scoped"] == "true"
