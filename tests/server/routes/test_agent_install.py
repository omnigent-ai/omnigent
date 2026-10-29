"""Agent installation, listing, detail, removal, and visibility.

A multi-user server must never let one user overwrite, see, remove, or bind
another user's agent, nor touch a server agent. These run against a strict
header-auth provider (no single-user ``"local"`` fallback), the posture of a
deployed server.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import builtin_agent_id, generate_agent_id, installed_agent_id
from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes._session_create_validation import require_user_agent_visible
from omnigent.server.routes.builtin_agents import create_builtin_agents_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.session import bundle_files
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


@pytest.mark.parametrize(
    ("blob_disappears", "config_edit"), [(False, True), (True, True), (False, False)]
)
async def test_reinstall_updates_in_place(
    client, agent_store, artifact_store, monkeypatch, blob_disappears: bool, config_edit: bool
) -> None:
    monkeypatch.setattr(type(agent_store), "supports_config_edit", config_edit)
    first = (await _install(client, ALICE, "orion", "v1")).json()
    if blob_disappears:
        original_get = artifact_store.get
        old_location = agent_store.get(first["id"]).bundle_location

        def get(location: str) -> bytes:
            if location == old_location:
                artifact_store.delete(location)
            return original_get(location)

        monkeypatch.setattr(artifact_store, "get", get)
    response = await _install(client, ALICE, "orion", "v2")
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["id"] == first["id"], "reinstall must keep the agent id stable"
    assert second["version"] == first["version"] + 1
    assert second["description"] == "v2", "reinstall must show the new bundle's description"
    assert [row["description"] for row in (await _mine(client, ALICE))["data"]] == ["v2"]


@pytest.mark.parametrize("with_etag", [True, False])
async def test_config_patch_preserves_identity_and_omitted_fields(
    client, agent_store, with_etag: bool
) -> None:
    installed = (await _install(client, ALICE, "orion")).json()
    path = f"/v1/agents/{installed['id']}"
    config = await client.get(f"{path}/config", headers=ALICE)
    assert config.status_code == 200, config.text
    assert config.headers["cache-control"] == "no-store"
    assert config.headers["access-control-expose-headers"] == "ETag"
    before = agent_store.get(installed["id"])
    headers = {**ALICE, **({"If-Match": config.headers["etag"]} if with_etag else {})}
    for patch in ({}, {"description": "v1"}):
        no_op = await client.patch(path, headers=headers, json=patch)
        assert no_op.status_code == 200, no_op.text
        assert no_op.headers["etag"] == config.headers["etag"]
        assert agent_store.get(installed["id"]) == before

    changed = await client.patch(path, headers=headers, json={"description": "updated"})
    assert changed.status_code == 200, changed.text
    assert changed.json() == {
        **config.json(),
        "description": "updated",
        "version": installed["version"] + 1,
    }
    assert changed.headers["etag"] != config.headers["etag"]
    stale = await client.patch(
        path, headers={**ALICE, "If-Match": config.headers["etag"]}, json={}
    )
    assert stale.status_code == 412
    cleared = await client.patch(
        path, headers={**ALICE, "If-Match": changed.headers["etag"]}, json={"description": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["description"] is None
    assert (await client.get(path, headers=ALICE)).json()["description"] is None
    reinstalled = await _install(client, ALICE, "orion", "reinstalled")
    assert reinstalled.json()["description"] == "reinstalled"


@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_local_config_rejects_legacy_source_tools(
    client, agent_store, artifact_store, monkeypatch, method
) -> None:
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    installed = await _install(client, ALICE, "orion")
    assert installed.status_code == 200, installed.text
    path = f"/v1/agents/{installed.json()['id']}"
    config = await client.get(f"{path}/config", headers=ALICE)
    assert config.status_code == 200, config.text
    agent = agent_store.get(installed.json()["id"])
    assert agent is not None
    legacy = bundle_files(
        {
            "config.yaml": b"spec_version: 1\nname: orion\n",
            "tools/python/echo.py": b"def echo(text: str) -> str:\n    return text\n",
        }
    )
    # Older installations can contain bundles current HTTP uploads would reject.
    artifact_store.put(agent.bundle_location, legacy)
    response = await client.request(
        method,
        f"{path}/config" if method == "GET" else path,
        headers={**ALICE, "If-Match": config.headers["etag"]},
        **({"json": {"description": "updated"}} if method == "PATCH" else {}),
    )
    assert response.status_code == 400, response.text
    assert "Invalid agent configuration" in response.text
    persisted = agent_store.get(agent.id)
    assert persisted is not None
    assert persisted.bundle_location == agent.bundle_location
    assert persisted.version == agent.version
    assert artifact_store.get(agent.bundle_location) == legacy


async def test_config_patch_checks_owner_before_preconditions(client, agent_store) -> None:
    owned = (await _install(client, BOB, "orion")).json()["id"]
    unowned = agent_store.create_user_agent(generate_agent_id(), "legacy", "missing", owner=None)
    server = agent_store.create(generate_agent_id(), "server", "missing")
    for agent_id in (owned, unowned.id, server.id, generate_agent_id()):
        for method, suffix, kwargs in (("GET", "/config", {}), ("PATCH", "", {"json": {}})):
            path = f"/v1/agents/{agent_id}{suffix}"
            assert (await client.request(method, path, headers=ALICE, **kwargs)).status_code == 404
            assert (await client.request(method, path, **kwargs)).status_code == 401


async def test_config_patch_preserves_effective_stored_description(client, agent_store) -> None:
    installed = (await _install(client, ALICE, "orion", "bundle description")).json()
    agent = agent_store.get(installed["id"])
    before = agent_store.update(agent.id, agent.bundle_location, description="stored description")
    path = f"/v1/agents/{agent.id}"
    config = await client.get(f"{path}/config", headers=ALICE)
    assert config.json()["description"] == "stored description"
    patched = await client.patch(
        path,
        headers={**ALICE, "If-Match": config.headers["etag"]},
        json={"description": config.json()["description"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json() == config.json()
    assert patched.headers["etag"] == config.headers["etag"]
    assert agent_store.get(agent.id) == before


async def test_config_read_reports_unsupported_format_and_missing_bundle(
    client, agent_store, artifact_store
) -> None:
    bundle = bundle_files(
        {"agent.yaml": b"name: old\nprompt: hi\nexecutor: {harness: claude-sdk}\n"}
    )
    installed = await client.post(
        "/v1/agents",
        headers=ALICE,
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert installed.status_code == 200, installed.text
    path = f"/v1/agents/{installed.json()['id']}/config"
    config = await client.get(path, headers=ALICE)
    assert config.status_code == 409, config.text
    assert config.json()["error"]["code"] == "agent_config_not_editable"
    agent = agent_store.get(installed.json()["id"])
    artifact_store.delete(agent.bundle_location)
    missing = await client.get(path, headers=ALICE)
    assert missing.status_code == 500
    assert "mcp_servers" not in missing.json()


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "renamed"},
        {"version": 100},
        {"model": None},
        {"model": " "},
        {"harness": None},
        {"mcp_servers": None},
        {"mcp_servers": {"docs": {"args": None}}},
        {"mcp_servers": {"docs": {"headers": {"Authorization": "[REDACTED]"}}}},
        {"description": {"private": "never-echo-this"}},
    ],
)
async def test_config_patch_rejects_invalid_fields_without_echoing_input(
    client, agent_store, patch: dict
) -> None:
    installed = (await _install(client, ALICE, "orion")).json()
    path = f"/v1/agents/{installed['id']}"
    config = await client.get(f"{path}/config", headers=ALICE)
    before = agent_store.get(installed["id"])
    result = await client.patch(
        path, headers={**ALICE, "If-Match": config.headers["etag"]}, json=patch
    )
    assert result.status_code == 422, result.text
    assert result.json() == {
        "error": {"code": "invalid_input", "message": "Invalid agent configuration patch"}
    }
    assert agent_store.get(installed["id"]) == before


async def test_config_patch_preconditions_and_body_limit(client, monkeypatch) -> None:
    from omnigent.server.routes import builtin_agents

    installed = (await _install(client, ALICE, "orion")).json()
    path = f"/v1/agents/{installed['id']}"
    config = await client.get(f"{path}/config", headers=ALICE)
    assert (await client.patch(path, headers=ALICE, json={})).status_code == 200
    for tag in ("", "*", "W/" + config.headers["etag"], config.headers["etag"] + ', "other"'):
        result = await client.patch(path, headers={**ALICE, "If-Match": tag}, json={})
        assert result.status_code == 400, result.text
    headers = {**ALICE, "If-Match": config.headers["etag"]}
    result = await client.patch(
        path, headers={**headers, "Content-Type": "text/plain"}, content="{}"
    )
    assert result.status_code == 415
    assert result.json()["error"]["code"] == "invalid_input"
    monkeypatch.setattr(builtin_agents, "MAX_INSTALL_BUNDLE_BYTES", 1024)
    result = await client.patch(path, headers=headers, json={"instructions": "x" * 2048})
    assert result.status_code == 413
    assert result.json()["error"]["code"] == "payload_too_large"
    assert result.headers["cache-control"] == "no-store"
    monkeypatch.setattr(builtin_agents, "MAX_INSTALL_BUNDLE_BYTES", 1)
    result = await client.get(f"{path}/config", headers=ALICE)
    assert result.status_code == 413
    assert result.json()["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize("with_etag", [True, False])
@pytest.mark.parametrize("race", ["update", "identical_update", "delete", "artifact_failure"])
async def test_config_patch_does_not_overwrite_racing_changes(
    client, agent_store, artifact_store, monkeypatch, race: str, with_etag: bool
) -> None:
    installed = (await _install(client, ALICE, "orion")).json()
    path = f"/v1/agents/{installed['id']}"
    config = await client.get(f"{path}/config", headers=ALICE)
    before = agent_store.get(installed["id"])
    original_put = artifact_store.put

    def put(location, data):
        if race == "artifact_failure":
            raise OSError("artifact storage unavailable")
        original_put(location, data)
        if race == "delete":
            agent_store.delete(installed["id"])
        elif race == "identical_update":
            agent_store.update(installed["id"], location, description="loser")
        else:
            agent_store.update(installed["id"], before.bundle_location)

    monkeypatch.setattr(artifact_store, "put", put)
    if race == "artifact_failure":
        with pytest.raises(OSError, match="artifact storage unavailable"):
            await client.patch(
                path,
                headers={**ALICE, **({"If-Match": config.headers["etag"]} if with_etag else {})},
                json={"description": "loser"},
            )
        assert agent_store.get(installed["id"]) == before
    else:
        result = await client.patch(
            path,
            headers={**ALICE, **({"If-Match": config.headers["etag"]} if with_etag else {})},
            json={"description": "loser"},
        )
        assert result.status_code == 412, result.text
        after = agent_store.get(installed["id"])
        if race == "delete":
            assert after is None
        else:
            if race == "identical_update":
                assert after.bundle_location != before.bundle_location
                assert artifact_store.get(after.bundle_location)
                config = await client.get(f"{path}/config", headers=ALICE)
                assert config.status_code == 200, config.text
                assert config.json()["description"] == "loser"
            else:
                assert after.bundle_location == before.bundle_location
            assert after.version == before.version + 1


async def test_agent_detail_returns_current_summary(client) -> None:
    for description in ("v1", "v2"):
        installed = (await _install(client, ALICE, "orion", description)).json()
        detail = await client.get(f"/v1/agents/{installed['id']}", headers=ALICE)
        assert detail.status_code == 200, detail.text
        assert installed == (await _mine(client, ALICE))["data"][0]
        assert detail.json() == {**installed, "user_owned": True}
        assert detail.json()["description"] == description

    removed = await client.delete(f"/v1/agents/{installed['id']}", headers=ALICE)
    assert removed.status_code == 200
    assert (await client.get(f"/v1/agents/{installed['id']}", headers=ALICE)).status_code == 404


async def test_agent_detail_requires_owner_and_authentication(client, agent_store) -> None:
    other = (await _install(client, BOB, "orion")).json()
    unowned = agent_store.create_user_agent(
        generate_agent_id(), "legacy", "test:///legacy", owner=None
    )
    for agent_id in (other["id"], unowned.id, generate_agent_id()):
        response = await client.get(f"/v1/agents/{agent_id}", headers=ALICE)
        assert response.status_code == 404
        assert response.json() == {"error": f"Agent not found: {agent_id!r}"}
        assert (await client.get(f"/v1/agents/{agent_id}")).status_code == 401


@pytest.mark.parametrize("seeded", [False, True])
async def test_server_agent_detail_is_shared(client, agent_store, seeded: bool) -> None:
    agent_id = builtin_agent_id("shared") if seeded else generate_agent_id()
    agent_store.create(agent_id, "shared", "test:///missing", description="Stored description")
    for headers in (ALICE, BOB):
        response = await client.get(f"/v1/agents/{agent_id}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {
            **(await client.get("/v1/agents", headers=headers)).json()["data"][0],
            "user_owned": False,
        }
        assert response.json()["builtin"] is seeded
        assert response.json()["description"] == "Stored description"
        assert response.json()["harness"] is None
    assert (await client.get(f"/v1/agents/{agent_id}")).status_code == 401


async def test_agent_detail_on_authless_server(agent_store, artifact_store, tmp_path) -> None:
    app = _app(agent_store, artifact_store, tmp_path, auth_provider=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        installed = (await _install(c, {}, "orion")).json()
        response = await c.get(f"/v1/agents/{installed['id']}")
        assert response.status_code == 200, response.text
        assert response.json() == {**installed, "user_owned": True}

        config = await c.get(f"/v1/agents/{installed['id']}/config")
        assert config.status_code == 200, config.text
        patched = await c.patch(
            f"/v1/agents/{installed['id']}",
            headers={"If-Match": config.headers["etag"]},
            json={"description": "authless edit"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["description"] == "authless edit"


async def test_agent_detail_redacts_mcp_headers(client, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DESCRIPTION", "server environment value")
    files = {
        "config.yaml": (
            "spec_version: 1\nname: orion\ndescription: ${AGENT_DESCRIPTION}\n"
            "executor:\n  config:\n    harness: claude-sdk\n"
        ),
        "tools/mcp/docs.yaml": (
            "name: docs\ntransport: http\nurl: https://docs.example.com/mcp\n"
            "headers:\n  Authorization: Bearer test-token\n"
        ),
    }
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as tar:
        for name, contents in files.items():
            data = contents.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    installed = await client.post(
        "/v1/agents",
        headers=ALICE,
        files={"bundle": ("bundle.tar.gz", bundle.getvalue(), "application/gzip")},
    )
    assert installed.status_code == 200, installed.text
    response = await client.get(f"/v1/agents/{installed.json()['id']}", headers=ALICE)
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "${AGENT_DESCRIPTION}"
    assert response.json()["mcp_servers"][0]["headers"] == {"Authorization": "[REDACTED]"}
    assert "test-token" not in response.text

    path = f"/v1/agents/{installed.json()['id']}"
    config = await client.get(f"{path}/config", headers=ALICE)
    assert config.status_code == 200, config.text
    assert config.json()["mcp_servers"]["docs"]["configured_header_names"] == ["Authorization"]
    assert "test-token" not in config.text
    patched = await client.patch(
        path,
        headers={**ALICE, "If-Match": config.headers["etag"]},
        json={"mcp_servers": {"docs": {"url": "https://example.com/mcp"}}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["mcp_servers"]["docs"]["configured_header_names"] == ["Authorization"]
    assert patched.json()["description"] == "${AGENT_DESCRIPTION}"
    assert "test-token" not in patched.text


def _retarred(bundle: bytes, mtime: int) -> bytes:
    """The same files tarred again, as the CLI does on every run: new timestamps,
    owner, and member order."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as src,
        gzip.GzipFile(fileobj=out, mode="wb", mtime=mtime) as gz,
        tarfile.open(fileobj=gz, mode="w") as dst,
    ):
        for member in reversed(src.getmembers()):
            data = src.extractfile(member) if member.isfile() else None
            member.mtime, member.uid, member.uname = mtime, 501, "runner"
            dst.addfile(member, data)
    return out.getvalue()


@pytest.mark.parametrize(
    "retar", [False, True], ids=["identical-bytes", "same-files-tarred-again"]
)
async def test_reinstalling_the_same_files_is_a_no_op(
    client, agent_store, artifact_store, retar: bool
) -> None:
    """However the client tars the same files, reinstalling them keeps the id and version."""
    bundle = build_agent_bundle("orion", description="same")
    installed = []
    for mtime in (1, 2):
        data = _retarred(bundle, mtime) if retar else bundle
        resp = await client.post(
            "/v1/agents",
            headers=ALICE,
            files={"bundle": ("bundle.tar.gz", data, "application/gzip")},
        )
        assert resp.status_code == 200, resp.text
        installed.append((resp.json()["id"], resp.json()["version"]))
    assert installed[1] == installed[0]
    assert installed[0][1] == 1
    location = agent_store.get(installed[0][0]).bundle_location
    assert artifact_store.get(location) == (_retarred(bundle, 1) if retar else bundle)


async def test_reinstalling_the_same_files_restores_a_lost_bundle(
    client, agent_store, artifact_store
) -> None:
    """A row can outlive its blob (pruned artifacts, a DB restored without its
    store); reinstalling the same files puts the blob back."""
    first = (await _install(client, ALICE, "orion", "same")).json()
    location = agent_store.get(first["id"]).bundle_location
    artifact_store.delete(location)

    again = (await _install(client, ALICE, "orion", "same")).json()
    assert (again["id"], again["version"]) == (first["id"], first["version"])
    assert artifact_store.exists(location)


async def test_same_name_for_another_user_is_a_separate_agent(client, agent_store) -> None:
    alice = (await _install(client, ALICE, "orion", "alice")).json()
    bob = (await _install(client, BOB, "orion", "bob")).json()
    assert bob["id"] != alice["id"]
    assert agent_store.get(alice["id"]).version == 1, "Bob's install touched Alice's agent"


@pytest.mark.parametrize("collision_aware", [False, True], ids=["legacy", "current"])
async def test_user_names_do_not_replace_same_named_server_agents(
    client, agent_store, collision_aware: bool
) -> None:
    shared = agent_store.create(generate_agent_id(), "orion", "server/bundle")
    installed = await client.post(
        "/v1/agents",
        headers=ALICE,
        data={"allow_server_name_collision": "true"} if collision_aware else {},
        files={"bundle": ("bundle.tar.gz", build_agent_bundle("orion"), "application/gzip")},
    )
    assert installed.status_code == (200 if collision_aware else 409), installed.text
    assert agent_store.get(shared.id).bundle_location == "server/bundle"
    expected = [installed.json()["id"]] if collision_aware else []
    assert shared.id not in expected
    assert [row["id"] for row in (await _mine(client, ALICE))["data"]] == expected


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


async def test_concurrent_child_upload_and_install_share_identity(
    multi_user_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = multi_user_client
    parent = await _upload(c, ALICE, build_agent_bundle("coordinator"))
    barrier = threading.Barrier(2, timeout=10)
    create = SqlAlchemyAgentStore.create_user_agent

    def racing_create(self, agent_id, name, bundle_location, owner, description=None):
        barrier.wait()
        return create(self, agent_id, name, bundle_location, owner, description)

    monkeypatch.setattr(SqlAlchemyAgentStore, "create_user_agent", racing_create)
    child, installed = await asyncio.gather(
        _upload(
            c,
            ALICE,
            build_agent_bundle("orion", description="v1"),
            {"parent_session_id": parent["session_id"]},
        ),
        _install(c, ALICE, "orion", "v1"),
    )
    assert installed.status_code == 200, installed.text
    assert installed.json()["id"] == child["agent_id"]
    assert installed.json()["version"] == 1
    store = SqlAlchemyAgentStore(db_uri)
    assert store.get(child["agent_id"]).version == 1
    assert [
        (a["id"], a["version"]) for a in (await _mine(c, ALICE))["data"] if a["name"] == "orion"
    ] == [(child["agent_id"], 1)]


async def test_install_creates_shared_identity_without_changing_legacy_uploads(
    client, agent_store, artifact_store
) -> None:
    bundle = build_agent_bundle("orion", description="v1")
    for agent_id in ("1" * 32, "2" * 32):
        location = f"{agent_id}/legacy"
        artifact_store.put(location, bundle)
        agent_store.create_user_agent(agent_id, "orion", location, "alice@example.com")
    agent_id = installed_agent_id("alice@example.com", "orion")
    same = await _install(client, ALICE, "orion", "v1")
    assert (same.json()["id"], same.json()["version"]) == (agent_id, 1)
    changed = await _install(client, ALICE, "orion", "v2")
    assert (changed.json()["id"], changed.json()["version"]) == (agent_id, 2)
    assert [a["id"] for a in (await _mine(client, ALICE))["data"]] == [agent_id]
    for legacy_id in ("1" * 32, "2" * 32):
        assert agent_store.get(legacy_id).version == 1


async def test_user_names_are_case_sensitive(client) -> None:
    lower = (await _install(client, ALICE, "orion", "lower")).json()
    upper = (await _install(client, ALICE, "Orion", "upper")).json()
    assert lower["id"] != upper["id"]
    updated = (await _install(client, ALICE, "orion", "changed")).json()
    assert (updated["id"], updated["version"]) == (lower["id"], 2)
    assert {(a["name"], a["version"]) for a in (await _mine(client, ALICE))["data"]} == {
        ("orion", 2),
        ("Orion", 1),
    }


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


async def test_store_without_user_agents_hides_the_routes(
    client, agent_store, monkeypatch
) -> None:
    mine = (await _install(client, ALICE, "orion")).json()
    shared = agent_store.create(generate_agent_id(), "shared", "test:///shared")
    monkeypatch.setattr(SqlAlchemyAgentStore, "supports_user_agents", property(lambda self: False))
    assert (await _install(client, ALICE, "orion")).status_code == 404
    listing = await client.get("/v1/agents", params={"scope": "user"}, headers=ALICE)
    assert listing.status_code == 404
    removal = await client.delete(f"/v1/agents/{generate_agent_id()}", headers=ALICE)
    assert removal.status_code == 404
    assert (await client.get("/v1/agents", headers=ALICE)).status_code == 200
    assert (await client.get(f"/v1/agents/{mine['id']}", headers=ALICE)).status_code == 404
    assert (await client.get(f"/v1/agents/{shared.id}", headers=ALICE)).status_code == 200


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

    for path, payload in (
        ("/v1/sessions", {"agent_id": orion["id"]}),
        (f"/v1/sessions/{alices.json()['id']}/fork", {}),
        (
            "/v1/scheduled-tasks",
            {"name": "nightly", "prompt": "go", "rrule": "FREQ=DAILY", "agent_id": orion["id"]},
        ),
    ):
        repeated = await c.post(path, json=payload, headers=BOB)
        assert repeated.status_code in (200, 201), repeated.text
        assert repeated.json()["agent_id"] == copy.id

    await _install(c, ALICE, "orion", "v2")
    assert agents.get(orion["id"]).bundle_location != orion_location
    assert agents.get(copy.id).bundle_location == copy.bundle_location

    for path, payload in (
        ("/v1/sessions", {"agent_id": orion["id"]}),
        (f"/v1/sessions/{alices.json()['id']}/fork", {}),
        (
            "/v1/scheduled-tasks",
            {"name": "later", "prompt": "go", "rrule": "FREQ=DAILY", "agent_id": orion["id"]},
        ),
    ):
        conflict = await c.post(path, json=payload, headers=BOB)
        assert conflict.status_code == 409, conflict.text
    assert agents.get(copy.id).bundle_location == copy.bundle_location
    assert [a["id"] for a in (await _mine(c, BOB))["data"]] == [copy.id]
    selected = await c.post(
        f"/v1/sessions/{alices.json()['id']}/fork", json={"agent_id": copy.id}, headers=BOB
    )
    assert selected.status_code == 201, selected.text
    assert selected.json()["agent_id"] == copy.id


@pytest.mark.parametrize("first", ["install", "upload", "child"])
async def test_all_creation_paths_update_one_owner_name(
    multi_user_client: httpx.AsyncClient, db_uri: str, first: str
) -> None:
    """The reported child-create then CLI-install flow updates one ID, in either order."""
    c = multi_user_client
    parent = await _upload(c, ALICE, build_agent_bundle("coordinator"))
    metadata = {"parent_session_id": parent["session_id"], "title": "helper"}
    bundle = build_agent_bundle("orion", description="v1")
    if first == "install":
        agent_id = (await _install(c, ALICE, "orion", "v1")).json()["id"]
    else:
        agent_id = (await _upload(c, ALICE, bundle, metadata if first == "child" else None))[
            "agent_id"
        ]
    original = SqlAlchemyAgentStore(db_uri).get(agent_id)
    session_ids = []
    for child in (False, True, True):
        session = await _upload(c, ALICE, _retarred(bundle, 42), metadata if child else None)
        session_ids.append(session["session_id"])
        assert session["agent_id"] == agent_id
        conv = SqlAlchemyConversationStore(db_uri).get_conversation(session["session_id"])
        assert conv.parent_conversation_id == (parent["session_id"] if child else None)
        assert conv.kind == ("sub_agent" if child else "default")
    assert len(set(session_ids)) == 3
    assert SqlAlchemyConversationStore(db_uri).count_sessions_for_agent(agent_id, 10) >= 3
    unchanged = await _install(c, ALICE, "orion", "v1")
    assert unchanged.status_code == 200, unchanged.text
    assert (unchanged.json()["id"], unchanged.json()["version"]) == (agent_id, 1)
    if first == "child":
        conflict = await c.post(
            "/v1/sessions",
            headers=ALICE,
            data={"metadata": json.dumps(metadata)},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle("orion", description="v2"),
                    "application/gzip",
                )
            },
        )
        assert conflict.status_code == 409, conflict.text
        unchanged_agent = SqlAlchemyAgentStore(db_uri).get(agent_id)
        assert (unchanged_agent.bundle_location, unchanged_agent.version) == (
            original.bundle_location,
            original.version,
        )
        assert all(
            SqlAlchemyConversationStore(db_uri).get_conversation(sid).agent_id == agent_id
            for sid in session_ids
        )
    if first in ("install", "child"):
        updated = await _install(c, ALICE, "orion", "v2")
        assert (updated.json()["id"], updated.json()["version"]) == (agent_id, 2)
    else:
        updated_session = await _upload(
            c,
            ALICE,
            build_agent_bundle("orion", description="v2"),
        )
        assert updated_session["agent_id"] == agent_id
    persisted = SqlAlchemyAgentStore(db_uri).get(agent_id)
    assert persisted.version == 2
    assert persisted.created_at == original.created_at
    assert persisted.bundle_location != original.bundle_location
    assert [
        (a["id"], a["version"]) for a in (await _mine(c, ALICE))["data"] if a["name"] == "orion"
    ] == [(agent_id, 2)]
    bobs = (await _install(c, BOB, "orion", "v3")).json()
    assert bobs["id"] != agent_id
    assert SqlAlchemyAgentStore(db_uri).get(bobs["id"]).created_by == "bob@example.com"
    assert (await c.get(f"/v1/agents/{agent_id}", headers=ALICE)).json()["description"] == "v2"


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    bundle: bytes,
    metadata: dict[str, str] | None = None,
) -> dict:
    """What ``omnigent run`` sends: a multipart session create carrying the bundle."""
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata or {})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_a_run_after_an_mcp_edit_starts_on_the_uploaded_files(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """An MCP edit reaches every session on the shared row, but a later run gets
    exactly the files it uploaded, updating that same row for every session."""
    c = multi_user_client
    bundle = build_agent_bundle("codex")
    first = await _upload(c, ALICE, bundle)
    server = {"name": "docs", "transport": "http", "url": "https://example.invalid/mcp"}
    added = await c.post(
        f"/v1/sessions/{first['session_id']}/agent/mcp-servers", json=server, headers=ALICE
    )
    assert added.status_code == 200, added.text

    later = [await _upload(c, ALICE, _retarred(bundle, mtime)) for mtime in (1, 2)]
    assert later[0]["agent_id"] == later[1]["agent_id"] == first["agent_id"]
    listed = await c.get(f"/v1/sessions/{later[0]['session_id']}/agent/mcp-servers", headers=ALICE)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


async def test_a_run_restores_its_agent_bundle_when_the_blob_is_gone(
    multi_user_client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    """A row can outlive its blob (pruned artifacts, a DB restored without its
    store). The next run of the same files binds that row and puts the blob
    back, so its session still loads the agent."""
    from omnigent.server.routes import sessions as session_routes
    from tests.server.helpers import policy_tool_call_request

    c = multi_user_client
    bundle = build_agent_bundle("codex")
    first = await _upload(c, ALICE, bundle)
    location = SqlAlchemyAgentStore(db_uri).get(first["agent_id"]).bundle_location
    LocalArtifactStore(str(tmp_path / "artifacts")).delete(location)
    # As on a fresh server: nothing cached to fall back on.
    session_routes.get_agent_cache().evict(first["agent_id"])

    rerun = await _upload(c, ALICE, _retarred(bundle, 1))
    assert rerun["agent_id"] == first["agent_id"]
    resp = await c.post(
        f"/v1/sessions/{rerun['session_id']}/policies/evaluate",
        json=policy_tool_call_request(),
        headers=ALICE,
    )
    assert resp.status_code == 200, resp.text


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


async def test_a_failed_session_create_keeps_a_copy_another_request_bound(
    multi_user_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bob's copy of Alice's agent is listed to him as soon as it exists, so another of
    his requests may bind it before this create fails; that session keeps its agent."""
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    c = multi_user_client
    orion = (await _install(c, ALICE, "orion")).json()
    alices = (await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=ALICE)).json()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user("bob@example.com")
    perms.grant("bob@example.com", alices["id"], 1)
    create = SqlAlchemyConversationStore.create_conversation
    others: list[str] = []

    def bind_then_fail(self: SqlAlchemyConversationStore, **kwargs: Any) -> None:
        others.append(create(self, **kwargs).id)  # the other request binds the copy first
        raise RuntimeError("conversation store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(SqlAlchemyConversationStore, "create_conversation", bind_then_fail)
        with pytest.raises(RuntimeError):
            await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=BOB)

    other = SqlAlchemyConversationStore(db_uri).get_conversation(others[0])
    assert other is not None and other.agent_id != orion["id"]
    assert SqlAlchemyAgentStore(db_uri).get(other.agent_id) is not None
    assert [row["id"] for row in (await _mine(c, BOB))["data"]] == [other.agent_id]


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("preexisting", [False, True])
async def test_failed_schedule_save_preserves_reusable_agents(
    multi_user_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    preexisting: bool,
) -> None:
    """Failed schedule writes never delete a shared owner/name identity."""
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
    bound_session = None
    if preexisting:
        bound_session = (
            await c.post("/v1/sessions", json={"agent_id": orion["id"]}, headers=BOB)
        ).json()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("task store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(SqlAlchemyScheduledTaskStore, operation, fail)
        with pytest.raises(RuntimeError):
            if operation == "update":
                await c.patch(
                    f"/v1/scheduled-tasks/{task.json()['id']}",
                    json={"agent_id": orion["id"]},
                    headers=BOB,
                )
            else:
                await c.post(
                    "/v1/scheduled-tasks",
                    json={
                        "name": "another",
                        "prompt": "go",
                        "rrule": "FREQ=DAILY",
                        "agent_id": orion["id"],
                    },
                    headers=BOB,
                )

    owned = {row["name"]: row["id"] for row in (await _mine(c, BOB))["data"]}
    assert set(owned) == {"vega", "orion"}
    if bound_session is not None:
        session = SqlAlchemyConversationStore(db_uri).get_conversation(bound_session["id"])
        assert session.agent_id == owned["orion"]
        assert SqlAlchemyAgentStore(db_uri).get(session.agent_id) is not None
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


@pytest.mark.parametrize("edit", ["reinstall", "patch", "upload"])
async def test_reinstall_reaches_every_session_on_its_next_turn(
    multi_user_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch, edit: str
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
        child = await _upload(
            c,
            ALICE,
            build_agent_bundle("orion", description="v1"),
            {"parent_session_id": sessions[0]["id"]},
        )
        sessions.append({"id": child["session_id"]})
        for session in sessions[1:]:
            sent = await c.post(
                f"/v1/sessions/{session['id']}/events", json=message, headers=ALICE
            )
            assert sent.status_code == 202, sent.text
        if edit == "patch":
            config = await c.get(f"/v1/agents/{orion['id']}/config", headers=ALICE)
            updated = await c.patch(
                f"/v1/agents/{orion['id']}",
                headers={**ALICE, "If-Match": config.headers["etag"]},
                json={"description": "v2"},
            )
            assert updated.status_code == 200, updated.text
        elif edit == "upload":
            await _upload(
                c,
                ALICE,
                build_agent_bundle("orion", description="v2"),
            )
        else:
            await _install(c, ALICE, "orion", "v2")
        for session in sessions:
            sent = await c.post(
                f"/v1/sessions/{session['id']}/events", json=message, headers=ALICE
            )
            assert sent.status_code == 202, sent.text

        new_session = await c.post("/v1/sessions", json=kickoff, headers=ALICE)
        assert new_session.status_code == 201, new_session.text

    v2 = SqlAlchemyAgentStore(db_uri).get(orion["id"]).bundle_location
    assert forwarded[new_session.json()["id"]] == [v2]
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
