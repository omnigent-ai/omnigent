"""Session MCP configuration edits require owner permission for every transport."""

from __future__ import annotations

import io
import socket
import tarfile
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
import yaml
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    UnifiedAuthProvider,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.util import ssrf

pytestmark = pytest.mark.asyncio

_OWNER = {"X-Forwarded-Email": "owner@example.com"}
_LEVELS = [
    pytest.param(LEVEL_OWNER, id="owner"),
    pytest.param(LEVEL_MANAGE, id="manager"),
    pytest.param(LEVEL_EDIT, id="editor"),
    pytest.param(LEVEL_READ, id="reader"),
]


@pytest.fixture()
def permission_store(db_uri: str) -> SqlAlchemyPermissionStore:
    """Share the permission store so grant changes invalidate its access cache."""
    return SqlAlchemyPermissionStore(db_uri)


@pytest.fixture()
def app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    permission_store: SqlAlchemyPermissionStore,
) -> FastAPI:
    """Use real session permissions and the deployed server's strict header auth."""
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)

    def resolve_test_host(
        host: str, *_args: object, **_kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "example.invalid"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", resolve_test_host)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


def _mcp_config(transport: Literal["http", "stdio"], *, changed: bool = False) -> dict[str, Any]:
    """Produce inert declarations whose credentials or environment can change."""
    value = "updated" if changed else "original"
    config: dict[str, Any] = {"name": "service", "transport": transport, "description": value}
    if transport == "http":
        config.update(url="https://example.invalid/mcp", headers={"X-Test-Config": value})
    else:
        config.update(command="test-mcp-must-never-run", args=[], env={"TEST_CONFIG": value})
    return config


def _bundle(
    mcp: dict[str, Any] | None = None,
    *,
    nested: bool = False,
    prompt: str = "Original instructions.",
) -> bytes:
    """Create a root or nested MCP declaration without connecting to it."""
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": "mcp-auth-agent",
        "executor": {"config": {"harness": "claude-sdk"}},
        "instructions": prompt,
    }
    files: dict[str, dict[str, Any]] = {"config.yaml": config}
    if nested:
        config["tools"] = {"agents": ["child"]}
        files["agents/child/config.yaml"] = {
            "spec_version": 1,
            "name": "child",
            "executor": {"config": {"harness": "claude-sdk"}},
        }
    if mcp is not None:
        prefix = "agents/child/" if nested else ""
        files[f"{prefix}tools/mcp/service.yaml"] = mcp
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, contents in files.items():
            data = yaml.safe_dump(contents).encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return result.getvalue()


async def _create_session(
    client: httpx.AsyncClient,
    db_uri: str,
    bundle: bytes,
    level: int,
) -> tuple[str, dict[str, str]]:
    """Create an owned session and grant the test caller its requested access."""
    created = await client.post(
        "/v1/sessions",
        headers=_OWNER,
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation is not None and conversation.agent_id is not None
    agent = SqlAlchemyAgentStore(db_uri).get(conversation.agent_id)
    assert agent is not None and agent.created_by == _OWNER["X-Forwarded-Email"]
    if level == LEVEL_OWNER:
        return session_id, _OWNER
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("collaborator@example.com")
    permissions.grant("collaborator@example.com", session_id, level)
    return session_id, {"X-Forwarded-Email": "collaborator@example.com"}


async def _download_bundle(client: httpx.AsyncClient, session_id: str) -> bytes:
    """Read the persisted bytes to ensure rejected requests cannot change them."""
    response = await client.get(f"/v1/sessions/{session_id}/agent/contents", headers=_OWNER)
    assert response.status_code == 200, response.text
    return response.content


@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_mcp_crud_requires_owner(
    client: httpx.AsyncClient,
    db_uri: str,
    level: int,
    transport: Literal["http", "stdio"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All collaborators may read MCPs; only owners may create, replace, or delete."""
    if transport == "stdio":
        monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    session_id, headers = await _create_session(client, db_uri, _bundle(), level)
    url = f"/v1/sessions/{session_id}/agent/mcp-servers"
    config = _mcp_config(transport)
    before = await _download_bundle(client, session_id)
    created = await client.post(url, headers=headers, json=config)
    assert created.status_code == (200 if level == LEVEL_OWNER else 403), created.text
    if level != LEVEL_OWNER:
        assert "needs owner permission" in created.text
        assert await _download_bundle(client, session_id) == before
        seeded = await client.post(url, headers=_OWNER, json=config)
        assert seeded.status_code == 200, seeded.text

    before = await _download_bundle(client, session_id)
    updated = await client.put(
        f"{url}/service", headers=headers, json=_mcp_config(transport, changed=True)
    )
    assert updated.status_code == (200 if level == LEVEL_OWNER else 403), updated.text
    if level != LEVEL_OWNER:
        assert "needs owner permission" in updated.text
        assert await _download_bundle(client, session_id) == before

    listed = await client.get(url, headers=headers)
    assert listed.status_code == 200, listed.text
    assert [server["description"] for server in listed.json()["data"]] == [
        "updated" if level == LEVEL_OWNER else "original"
    ]

    before = await _download_bundle(client, session_id)
    deleted = await client.delete(f"{url}/service", headers=headers)
    assert deleted.status_code == (204 if level == LEVEL_OWNER else 403), deleted.text
    if level != LEVEL_OWNER:
        assert "needs owner permission" in deleted.text
        assert await _download_bundle(client, session_id) == before
    listed = await client.get(url, headers=headers)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["data"]) == (0 if level == LEVEL_OWNER else 1)


@pytest.mark.parametrize("level", _LEVELS)
async def test_shared_stdio_policy_preserves_owner_only_removal(
    client: httpx.AsyncClient,
    db_uri: str,
    level: int,
) -> None:
    """Shared servers reject stdio registration while owners can remove old entries."""
    initial = _bundle(_mcp_config("stdio"))
    session_id, headers = await _create_session(client, db_uri, initial, level)
    url = f"/v1/sessions/{session_id}/agent/mcp-servers"
    for method, path in (("POST", url), ("PUT", f"{url}/service")):
        response = await client.request(
            method, path, headers=headers, json=_mcp_config("stdio", changed=True)
        )
        assert response.status_code == 403, response.text
        expected = (
            "stdio MCP servers are not permitted"
            if level == LEVEL_OWNER
            else "needs owner permission"
        )
        assert expected in response.text
        assert await _download_bundle(client, session_id) == initial
    deleted = await client.delete(f"{url}/service", headers=headers)
    assert deleted.status_code == (204 if level == LEVEL_OWNER else 403), deleted.text
    if level != LEVEL_OWNER:
        assert await _download_bundle(client, session_id) == initial


@pytest.mark.parametrize("level", [LEVEL_EDIT, LEVEL_MANAGE], ids=["editor", "manager"])
async def test_agent_creator_still_needs_session_ownership(
    client: httpx.AsyncClient,
    db_uri: str,
    permission_store: SqlAlchemyPermissionStore,
    level: int,
) -> None:
    """Creating the agent does not authorize edits after losing session ownership."""
    initial = _bundle(_mcp_config("http"))
    session_id, headers = await _create_session(client, db_uri, initial, LEVEL_OWNER)
    permission_store.grant(_OWNER["X-Forwarded-Email"], session_id, level)
    url = f"/v1/sessions/{session_id}/agent/mcp-servers"
    for method, path in (("POST", url), ("PUT", f"{url}/service"), ("DELETE", f"{url}/service")):
        response = await client.request(
            method, path, headers=headers, json=_mcp_config("http", changed=True)
        )
        assert response.status_code == 403, response.text
        assert "needs owner permission" in response.text
        assert await _download_bundle(client, session_id) == initial
    replaced = await client.put(
        f"/v1/sessions/{session_id}/agent",
        headers=headers,
        files={"bundle": ("agent.tar.gz", _bundle(prompt="Changed."), "application/gzip")},
    )
    assert replaced.status_code == 403, replaced.text
    assert "needs owner permission" in replaced.text
    assert await _download_bundle(client, session_id) == initial


@pytest.mark.parametrize(
    "level",
    [LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE],
    ids=["reader", "editor", "manager"],
)
async def test_child_agent_metadata_requires_effective_session_owner(
    client: httpx.AsyncClient,
    db_uri: str,
    permission_store: SqlAlchemyPermissionStore,
    level: int,
) -> None:
    """A creator with inherited non-owner access sees read-only child MCP metadata."""
    session_id, headers = await _create_session(
        client,
        db_uri,
        _bundle(_mcp_config("http")),
        LEVEL_OWNER,
    )
    parent = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert parent is not None and parent.agent_id is not None
    child = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id=parent.agent_id,
        kind="sub_agent",
        parent_conversation_id=session_id,
        sub_agent_name="child",
    )
    permission_store.grant(_OWNER["X-Forwarded-Email"], session_id, level)

    response = await client.get(f"/v1/sessions/{child.id}/agent", headers=headers)

    assert response.status_code == 200, response.text
    assert [server["name"] for server in response.json()["mcp_servers"]] == ["service"]
    assert response.json()["mcp_servers_editable"] is False


async def test_child_agent_metadata_allows_effective_session_owner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The parent owner and agent creator retain MCP controls on a child."""
    session_id, headers = await _create_session(
        client,
        db_uri,
        _bundle(_mcp_config("http")),
        LEVEL_OWNER,
    )
    parent = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert parent is not None and parent.agent_id is not None
    child = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id=parent.agent_id,
        kind="sub_agent",
        parent_conversation_id=session_id,
        sub_agent_name="child",
    )

    response = await client.get(f"/v1/sessions/{child.id}/agent", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["mcp_servers_editable"] is True


@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("nested", [False, True], ids=["root", "subagent"])
async def test_bundle_replacement_requires_owner(
    client: httpx.AsyncClient,
    db_uri: str,
    level: int,
    transport: Literal["http", "stdio"],
    nested: bool,
) -> None:
    """Only owners may replace a bundle, including prompt-only and nested MCP edits."""
    config = _mcp_config(transport)
    initial = _bundle(config, nested=nested)
    session_id, headers = await _create_session(client, db_uri, initial, level)
    url = f"/v1/sessions/{session_id}/agent"
    prompt_edit = _bundle(config, nested=nested, prompt="Updated instructions.")
    edited = await client.put(
        url,
        headers=headers,
        files={"bundle": ("agent.tar.gz", prompt_edit, "application/gzip")},
    )
    assert edited.status_code == (200 if level == LEVEL_OWNER else 403), edited.text
    expected = prompt_edit if level == LEVEL_OWNER else initial
    assert await _download_bundle(client, session_id) == expected

    changed_config = _mcp_config(transport, changed=True)
    changed_config["description"] = config["description"]
    mcp_edit = _bundle(changed_config, nested=nested, prompt="Updated instructions.")
    edited = await client.put(
        url,
        headers=headers,
        files={"bundle": ("agent.tar.gz", mcp_edit, "application/gzip")},
    )
    assert edited.status_code == (200 if level == LEVEL_OWNER else 403), edited.text
    assert await _download_bundle(client, session_id) == (
        mcp_edit if level == LEVEL_OWNER else expected
    )
