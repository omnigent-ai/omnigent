"""READ callers receive a redacted bundle; editors retain the original."""

import asyncio
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.utils import clear_engine_cache
from omnigent.errors import OmnigentError
from omnigent.runner.tool_dispatch import _execute_agent_tool
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.bundles import copy_agent_bundle, redacted_agent_bundle, validate_agent_bundle
from omnigent.server.routes.sessions.routes_agent import register_agent_routes
from omnigent.server.routes.sessions.routes_core import register_core_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def _source_bundle(
    agent_name: str, secret: str, *, executor: dict[str, object] | None = None
) -> bytes:
    if executor is None:
        executor = {
            "type": "omnigent",
            "model": "databricks-claude-opus-4-6",
            "config": {"harness": "cursor", "profile": secret},
        }
    config = json.dumps(
        {
            "spec_version": 1,
            "name": agent_name,
            "description": secret,
            "instructions": secret,
            "executor": executor,
        }
    ).encode()
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        for name, content in (("config.yaml", config), ("private.txt", secret.encode())):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return source.getvalue()


@pytest.mark.parametrize("level", [LEVEL_READ, LEVEL_EDIT, LEVEL_OWNER])
def test_read_only_agent_bundle_is_redacted(tmp_path: Path, level: int) -> None:
    """READ gets a redacted bundle; EDIT and above get the original."""
    uri = f"sqlite:///{tmp_path / 'agent-bundle.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    permissions = SqlAlchemyPermissionStore(uri)
    agents = SqlAlchemyAgentStore(uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent = agents.create("a" * 32, "bundle-authority-test", "agents/bundle.tar.gz")
    secret = "SOURCE_PRIVATE_CREDENTIAL_23938"
    bundle = _source_bundle(agent.name, secret)
    artifacts.put(agent.bundle_location, bundle)
    conversation = conversations.create_conversation(agent_id=agent.id)
    permissions.grant("collaborator@example.com", conversation.id, level)

    router = APIRouter()
    register_agent_routes(
        router,
        conversation_store=conversations,
        agent_store=agents,
        artifact_store=artifacts,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=permissions,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, error: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content={"error": str(error)})

    app.include_router(router, prefix="/v1")
    try:
        with TestClient(app, headers={"X-Forwarded-Email": "collaborator@example.com"}) as client:
            response = client.get(f"/v1/sessions/{conversation.id}/agent/contents")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        if level == LEVEL_READ:
            assert response.headers["x-agent-bundle-redacted"] == "true"
            assert secret.encode() not in response.content
            with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
                assert archive.getnames() == ["config.yaml"]
                config = archive.extractfile("config.yaml")
                assert config is not None
                assert json.load(config) == {
                    "spec_version": 1,
                    "name": agent.name,
                    "executor": {"type": "omnigent", "config": {"harness": "cursor"}},
                }
            redacted_spec = validate_agent_bundle(response.content)
            assert redacted_spec.name == agent.name
            assert redacted_spec.executor.harness_kind == "cursor"
        else:
            assert response.headers["x-agent-bundle-redacted"] == "false"
            assert response.content == bundle

        workspace = tmp_path / "runner"

        async def download_via_tool() -> dict[str, object]:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"X-Forwarded-Email": "collaborator@example.com"},
            ) as server_client:
                result = await _execute_agent_tool(
                    "sys_agent_download",
                    {"session_id": conversation.id},
                    server_client=server_client,
                    agent_spec=None,
                    conversation_id=conversation.id,
                    runner_workspace=workspace,
                )
            return json.loads(result)

        tool_result = asyncio.run(download_via_tool())
        assert Path(str(tool_result["path"])).read_bytes() == response.content
        assert tool_result["redacted"] is (level == LEVEL_READ)
    finally:
        clear_engine_cache()


def test_fork_copy_can_omit_every_source_file(tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    secret = "private credential in an arbitrary bundle file"
    source = _source_bundle("bundle-authority-test", secret)
    artifacts.put("agent/source", source)

    location = copy_agent_bundle(
        artifacts, "agent/source", "fork-agent", redacted_name="bundle-authority-test"
    )
    copied = artifacts.get(location)
    assert copied is not None
    with tarfile.open(fileobj=io.BytesIO(copied), mode="r:gz") as archive:
        assert archive.getnames() == ["config.yaml"]
        config = archive.extractfile("config.yaml")
        assert config is not None
        assert json.load(config) == {
            "spec_version": 1,
            "name": "bundle-authority-test",
            "executor": {"type": "omnigent", "config": {"harness": "cursor"}},
        }
    assert validate_agent_bundle(copied).executor.harness_kind == "cursor"


def test_invalid_source_does_not_expose_config_in_redaction_error() -> None:
    secret = "SOURCE_PRIVATE_CREDENTIAL_INVALID"
    source = io.BytesIO()
    config = f"spec_version: 1\nname: agent\ninstructions: [{secret}".encode()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        member = tarfile.TarInfo("config.yaml")
        member.size = len(config)
        archive.addfile(member, io.BytesIO(config))

    with pytest.raises(OmnigentError, match="Agent bundle cannot be redacted") as exc:
        redacted_agent_bundle("agent", source.getvalue())
    assert secret not in str(exc.value)


def test_redaction_retains_non_omnigent_executor_type() -> None:
    source = _source_bundle("sdk-agent", "private credential", executor={"type": "claude_sdk"})

    redacted = redacted_agent_bundle("sdk-agent", source)

    with tarfile.open(fileobj=io.BytesIO(redacted), mode="r:gz") as archive:
        assert archive.getnames() == ["config.yaml"]
        config = archive.extractfile("config.yaml")
        assert config is not None
        assert json.load(config) == {
            "spec_version": 1,
            "name": "sdk-agent",
            "executor": {"type": "claude_sdk"},
        }
    assert validate_agent_bundle(redacted).executor.harness_kind == "claude_sdk"


@pytest.mark.parametrize("level", [LEVEL_READ, LEVEL_EDIT])
def test_fork_agent_copy_is_redacted_only_for_read(tmp_path: Path, level: int) -> None:
    """A READ fork gets a redacted agent copy; an EDIT fork keeps the full bundle."""
    uri = f"sqlite:///{tmp_path / 'fork-bundle.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    permissions = SqlAlchemyPermissionStore(uri)
    agents = SqlAlchemyAgentStore(uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent = agents.create("b" * 32, "shared-agent", "agents/shared.tar.gz")
    secret = "SOURCE_PRIVATE_CREDENTIAL_FORK"
    original = _source_bundle(agent.name, secret)
    artifacts.put(agent.bundle_location, original)
    source = conversations.create_conversation(agent_id=agent.id)
    permissions.grant("collaborator@example.com", source.id, level)

    router = APIRouter()
    register_core_routes(
        router,
        conversation_store=conversations,
        agent_store=agents,
        artifact_store=artifacts,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=permissions,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, error: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content={"error": str(error)})

    app.include_router(router, prefix="/v1")
    try:
        with TestClient(app, headers={"X-Forwarded-Email": "collaborator@example.com"}) as client:
            response = client.post(f"/v1/sessions/{source.id}/fork", json={})
        assert response.status_code == 201, response.text
        fork = conversations.get_conversation(response.json()["id"])
        assert fork is not None
        assert fork.agent_id is not None
        if level == LEVEL_EDIT:
            # An editor's fork shares the source agent and its original bundle.
            assert fork.agent_id == source.agent_id
            assert artifacts.get(agent.bundle_location) == original
            return
        assert fork.agent_id != source.agent_id
        fork_agent = agents.get(fork.agent_id)
        assert fork_agent is not None
        bundle = artifacts.get(fork_agent.bundle_location)
        assert bundle is not None
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
            assert archive.getnames() == ["config.yaml"]
            config = archive.extractfile("config.yaml")
            assert config is not None
            assert json.load(config) == {
                "spec_version": 1,
                "name": agent.name,
                "executor": {"type": "omnigent", "config": {"harness": "cursor"}},
            }
        assert validate_agent_bundle(bundle).executor.harness_kind == "cursor"
    finally:
        clear_engine_cache()
