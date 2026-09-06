"""Private library ownership and full-archive lifecycle regression coverage."""

from __future__ import annotations

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml
from starlette.requests import HTTPConnection

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.custom_agent_bundles import patch_bundle
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.routes import custom_agents as custom_agents_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


def bundle(config: str | None = None) -> bytes:
    entries = {
        "config.yaml": (
            config
            or """spec_version: 1
name: custom-reviewer
description: Original description
executor:
  type: omnigent
  model: test-model
  config:
    harness: codex
instructions: prompts/custom.md
tools:
  remote:
    type: mcp
    url: https://example.invalid/mcp
    headers:
      Authorization: '${CATALOG_TEST_TOKEN}'
"""
        ).encode(),
        "prompts/custom.md": b"Original instructions",
        "tools/helper.py": b"# Preserve this bundled executable verbatim\n",
        "assets/data.bin": bytes(range(256)),
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".py") else 0o644
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def members(data: bytes) -> dict[str, tuple[bytes, int]]:
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {m.name: (archive.extractfile(m).read(), m.mode) for m in archive if m.isfile()}


def make_app(db_uri: str, tmp_path: Path):
    artifacts = LocalArtifactStore(str(tmp_path / "custom-artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    app = create_app(
        agents,
        SqlAlchemyFileStore(db_uri),
        conversations,
        artifacts,
        AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "custom-cache"),
        auth_provider=HeaderAuth(),
        permission_store=permissions,
    )
    return app, artifacts, agents, conversations, permissions


@pytest.mark.asyncio
async def test_private_crud_archive_and_existing_session_survive(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATALOG_TEST_TOKEN", "must-not-expand")
    app, artifacts, agents, conversations, permissions = make_app(db_uri, tmp_path)
    alice, bob = {"x-test-user": "alice"}, {"x-test-user": "bob"}
    original = bundle()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/custom-agents")).status_code == 401
        created = await client.post(
            "/v1/custom-agents", headers=alice, files={"bundle": ("agent.tar.gz", original)}
        )
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["instructions"] == "Original instructions"
        path = f"/v1/custom-agents/{row['id']}"
        assert (await client.get("/v1/agents", headers=alice)).json()["data"] == []
        assert (await client.get("/v1/custom-agents", headers=bob)).json()["data"] == []
        for suffix in ("", "/contents"):
            assert (await client.get(path + suffix, headers=bob)).status_code == 404
        assert (await client.patch(path, headers=bob, json={"name": "stolen"})).status_code == 404
        assert (await client.delete(path, headers=bob)).status_code == 404
        assert (await client.get(path + "/contents", headers=alice)).content == original

        # A real runtime snapshot uses its own agent id and retained bundle key.
        runtime_id = generate_agent_id()
        location = bundle_location(runtime_id, original)
        artifacts.put(location, original)
        snapshot = conversations.create_session_with_agent(
            agent_id=runtime_id,
            agent_name="custom-reviewer",
            agent_bundle_location=location,
            agent_description=None,
            labels={"omnigent:agent-template-id": row["id"]},
        )
        permissions.grant("alice", snapshot.conversation.id, LEVEL_OWNER)
        edited = await client.patch(
            path,
            headers=alice,
            json={
                "name": "Renamed",
                "description": None,
                "instructions": "assets/data.bin",
                "version": 1,
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["instructions"] == "assets/data.bin"
        assert edited.json()["version"] == 2
        downloaded = (await client.get(path + "/contents", headers=alice)).content
        before, after = members(original), members(downloaded)
        for name in before.keys() - {"config.yaml"}:
            assert after[name] == before[name]
        assert b"${CATALOG_TEST_TOKEN}" in after["config.yaml"][0]
        assert b"must-not-expand" not in downloaded
        assert (
            await client.patch(path, headers=alice, json={"name": "stale", "version": 1})
        ).status_code == 409
        assert (await client.delete(path, headers=alice)).status_code == 204
        assert (await client.get(path, headers=alice)).status_code == 404
        assert (await client.get("/v1/custom-agents", headers=alice)).json()["data"] == []
        runtime = agents.get(runtime_id)
        assert runtime is not None and artifacts.get(runtime.bundle_location) == original
        existing = await client.get(
            f"/v1/sessions/{snapshot.conversation.id}/agent/contents", headers=alice
        )
        assert existing.status_code == 200 and existing.content == original
        assert existing.headers["x-agent-session-scoped"] == "true"


@pytest.mark.asyncio
async def test_import_requires_owner_and_retains_archive(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, artifacts, agents, conversations, permissions = make_app(db_uri, tmp_path)
    original = bundle()
    runtime_id = generate_agent_id()
    location = bundle_location(runtime_id, original)
    artifacts.put(location, original)
    snapshot = conversations.create_session_with_agent(
        agent_id=runtime_id,
        agent_name="custom-reviewer",
        agent_bundle_location=location,
        agent_description=None,
    )
    session_id = snapshot.conversation.id
    permissions.grant("alice", session_id, LEVEL_OWNER)
    permissions.grant("bob", session_id, LEVEL_READ)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = {"source_session_id": session_id}
        assert (
            await client.post("/v1/custom-agents", headers={"x-test-user": "bob"}, json=payload)
        ).status_code == 403
        created = await client.post(
            "/v1/custom-agents", headers={"x-test-user": "alice"}, json=payload
        )
        assert created.status_code == 201, created.text
        contents = await client.get(
            f"/v1/custom-agents/{created.json()['id']}/contents", headers={"x-test-user": "alice"}
        )
        assert contents.content == original
        assert agents.get(runtime_id) is not None
        template_id = created.json()["id"]
        assert (
            conversations.get_conversation(session_id).labels["omnigent:agent-template-id"]
            == template_id
        )
        deleted = await client.delete(
            f"/v1/custom-agents/{template_id}", headers={"x-test-user": "alice"}
        )
        assert deleted.status_code == 204
        assert (
            conversations.get_conversation(session_id).labels["omnigent:agent-template-id"]
            == template_id
        )
        assert artifacts.get(location) == original
        bad_type = await client.post(
            "/v1/custom-agents",
            headers={"x-test-user": "alice", "content-type": "text/plain"},
            content="{}",
        )
        assert bad_type.status_code == 415


def test_patch_block_scalar_keeps_following_yaml_and_empty_instructions() -> None:
    original = bundle("""spec_version: 1
name: test
description: |
  Long description
executor:
  type: omnigent
  config:
    harness: codex
instructions: prompts/custom.md
""")
    updated = patch_bundle(original, {"description": "Short", "instructions": None})
    spec = validate_agent_bundle(updated)
    assert spec.description == "Short" and spec.instructions == ""
    assert spec.executor.harness_kind == "codex"


@pytest.mark.parametrize("trailing_comma", [False, True])
def test_patch_flow_root_adds_fields_inside_mapping(trailing_comma: bool) -> None:
    config = (
        "{spec_version: 1, name: custom-reviewer, executor: "
        "{type: omnigent, config: {harness: codex}}"
        + (", # Keep this comment\n" if trailing_comma else "")
        + "}\n"
    )
    original = bundle(config)
    updated = patch_bundle(
        original, {"description": "New description", "instructions": "New text"}
    )
    spec = validate_agent_bundle(updated)
    assert spec.description == "New description" and spec.instructions == "New text"
    before, after = members(original), members(updated)
    for name in before.keys() - {"config.yaml"}:
        assert after[name] == before[name]


@pytest.mark.parametrize("flow", [False, True])
def test_patch_anchored_scalar_preserves_unrelated_alias_values(flow: bool) -> None:
    config = (
        "{spec_version: 1, name: &title custom-reviewer, description: *title, "
        "executor: {type: omnigent, model: *title, config: {harness: codex}}, "
        "instructions: 'Keep ${UNEXPANDED_VALUE}'}\n"
        if flow
        else "spec_version: 1\nname: &title custom-reviewer\ndescription: *title\n"
        "executor:\n  type: omnigent\n  model: *title\n  config:\n    harness: codex\n"
        "instructions: 'Keep ${UNEXPANDED_VALUE}'\n"
    )
    updated = patch_bundle(bundle(config), {"name": "new-name"})
    spec = validate_agent_bundle(updated)
    assert spec.name == "new-name"
    assert spec.description == "custom-reviewer"
    assert spec.executor.model == "custom-reviewer"
    assert spec.instructions == "Keep ${UNEXPANDED_VALUE}"


def test_patch_alias_value_and_document_end_preserve_other_fields() -> None:
    original = bundle("""spec_version: 1
name: &title custom-reviewer
description: *title
executor:
  type: omnigent
  config:
    harness: codex
...
""")
    updated = patch_bundle(original, {"description": "Changed", "instructions": "Added"})
    spec = validate_agent_bundle(updated)
    assert spec.name == "custom-reviewer" and spec.description == "Changed"
    assert spec.instructions == "Added"
    parsed = yaml.safe_load(members(updated)["config.yaml"][0])
    assert parsed["executor"] == {"type": "omnigent", "config": {"harness": "codex"}}


def test_patch_replaces_pax_sized_member_without_truncation() -> None:
    raw = b"""spec_version: 1
name: old
executor:
  type: omnigent
  config:
    harness: codex
"""
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("config.yaml")
        info.size = len(raw)
        info.pax_headers = {"size": str(len(raw))}
        archive.addfile(info, io.BytesIO(raw))

    new_name = "a-name-longer-than-the-original-pax-sized-config-member"
    updated = patch_bundle(archive_bytes.getvalue(), {"name": new_name})

    assert validate_agent_bundle(updated).name == new_name
    config = members(updated)["config.yaml"][0]
    assert yaml.safe_load(config)["name"] == new_name


def test_repeated_instruction_edits_reuse_generated_member() -> None:
    once = patch_bundle(bundle(), {"instructions": "First revision"})
    twice = patch_bundle(once, {"instructions": "Second revision"})

    generated = [
        name for name in members(twice) if name.startswith("catalog-instructions-")
    ]
    assert len(generated) == 1
    assert members(twice)[generated[0]][0] == b"Second revision"
    assert validate_agent_bundle(twice).instructions == "Second revision"


@pytest.mark.asyncio
async def test_chunked_multipart_stops_reading_after_request_limit(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    monkeypatch.setattr(custom_agents_routes, "MAX_MULTIPART_REQUEST_BYTES", 1024)
    boundary = "catalog-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="bundle"; filename="agent.tar.gz"\r\n'
        "Content-Type: application/gzip\r\n\r\n"
    ).encode()
    chunks = [prefix, *([b"x" * 256] * 20), f"\r\n--{boundary}--\r\n".encode()]
    yielded = 0

    async def stream() -> AsyncIterator[bytes]:
        nonlocal yielded
        for chunk in chunks:
            yielded += 1
            yield chunk

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/custom-agents",
            headers={
                "x-test-user": "alice",
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
            content=stream(),
        )

    assert response.status_code == 413
    assert yielded < len(chunks)


@pytest.mark.asyncio
async def test_detail_validates_bundle_only_once_per_artifact(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    original = bundle()
    agent_id = "ca_cache_probe"
    location = bundle_location(agent_id, original)
    artifacts.put(location, original)
    CustomAgentsStore(db_uri).create(
        "alice",
        {
            "id": agent_id,
            "name": "custom-reviewer",
            "description": "Original description",
            "harness": "codex",
            "model": "test-model",
            "bundle_location": location,
        },
    )
    original_get = artifacts.get
    reads = 0

    def counted_get(key: str) -> bytes:
        nonlocal reads
        reads += 1
        return original_get(key)

    monkeypatch.setattr(artifacts, "get", counted_get)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/v1/custom-agents/{agent_id}"
        first = await client.get(path, headers={"x-test-user": "alice"})
        second = await client.get(path, headers={"x-test-user": "alice"})

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["instructions"] == "Original instructions"
    assert reads == 1


@pytest.mark.asyncio
async def test_same_content_patch_cas_keeps_winner_bundle(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    headers = {"x-test-user": "alice"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/custom-agents",
            headers=headers,
            files={"bundle": ("agent.tar.gz", bundle())},
        )
        assert created.status_code == 201
        path = f"/v1/custom-agents/{created.json()['id']}"
        payload = {"name": "Concurrent_winner", "version": 1}
        results = await asyncio.gather(
            client.patch(path, headers=headers, json=payload),
            client.patch(path, headers=headers, json=payload),
        )

        assert sorted(response.status_code for response in results) == [200, 409], [
            (response.status_code, response.text) for response in results
        ]
        contents = await client.get(path + "/contents", headers=headers)
        assert contents.status_code == 200
        assert validate_agent_bundle(contents.content).name == "Concurrent_winner"


@pytest.mark.asyncio
async def test_custom_upload_rejects_untrusted_browser_origin(
    db_uri: str, tmp_path: Path, runtime_init: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _artifacts, _agents, _conversations, _permissions = make_app(db_uri, tmp_path)
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for origin, expected in [
            ("https://untrusted.invalid", 403),
            ("http://localhost:5187", 201),
        ]:
            response = await client.post(
                "/v1/custom-agents",
                headers={"x-test-user": "alice", "origin": origin},
                files={"bundle": ("agent.tar.gz", bundle())},
            )
            assert response.status_code == expected, response.text
