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
from omnigent.server.routes.sessions import routes_agent
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
def test_read_only_agent_bundle_is_redacted(
    tmp_path: Path, level: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """READ gets a redacted bundle; EDIT and above get the original."""
    monkeypatch.setattr(routes_agent, "local_single_user_enabled", lambda: False)
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
    app.state.policy_modules = ["example_org.policies.gate"]

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, error: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content={"error": str(error)})

    app.include_router(router, prefix="/v1")
    try:
        with TestClient(app, headers={"X-Forwarded-Email": "collaborator@example.com"}) as client:
            response = client.get(f"/v1/sessions/{conversation.id}/agent/contents")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        # The runner takes handler authority from these headers.
        assert json.loads(response.headers["x-omnigent-policy-modules"]) == [
            "example_org.policies.gate"
        ]
        assert response.headers["x-omnigent-local-single-user"] == "0"
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


def test_only_an_older_runner_loading_the_spec_gets_the_tool_group_opt_out_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An author's ``tool_groups.gadgets: false`` stays in every export, sys_agent_download
    included; only a runner that loads the spec without naming the group gets it dropped,
    since its parser would refuse the key and it has no gadget tools anyway."""
    import yaml

    from omnigent.automations_availability import ACCEPTED_TOOL_GROUPS_HEADER
    from omnigent.mcp_selection import MCP_SELECTION_HEADER
    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, RUNNER_TUNNEL_TOKEN_HEADER

    monkeypatch.setattr(routes_agent, "local_single_user_enabled", lambda: False)
    uri = f"sqlite:///{tmp_path / 'agent-bundle.db'}"
    conversations = SqlAlchemyConversationStore(uri)
    permissions = SqlAlchemyPermissionStore(uri)
    agents = SqlAlchemyAgentStore(uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent = agents.create("b" * 32, "opted-out", "agents/opted-out.tar.gz")
    config = json.dumps(
        {
            "spec_version": 1,
            "name": agent.name,
            "instructions": "Help.",
            "executor": {"type": "omnigent", "config": {"harness": "cursor"}},
            "tool_groups": {"gadgets": False},
        }
    ).encode()
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        member = tarfile.TarInfo("config.yaml")
        member.size = len(config)
        archive.addfile(member, io.BytesIO(config))
    bundle = source.getvalue()
    artifacts.put(agent.bundle_location, bundle)
    conversation = conversations.create_conversation(agent_id=agent.id)
    permissions.grant("author@example.com", conversation.id, LEVEL_EDIT)

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
    app.include_router(router, prefix="/v1")

    def tool_groups(content: bytes) -> object:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            data = archive.extractfile("config.yaml")
            assert data is not None
            return yaml.safe_load(data).get("tool_groups")

    path = f"/v1/sessions/{conversation.id}/agent/contents"
    try:
        with TestClient(app, headers={"X-Forwarded-Email": "author@example.com"}) as client:
            exported = client.get(path)
            older_runner = client.get(path, headers={MCP_SELECTION_HEADER: "1"})
            current_runner = client.get(
                path, headers={MCP_SELECTION_HEADER: "1", ACCEPTED_TOOL_GROUPS_HEADER: "gadgets"}
            )
            # A host reading the session's skills parses the spec too: one from before the
            # group sends neither header, only its first-party Origin.
            older_host = client.get(path, headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN})
            current_host = client.get(
                path,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    ACCEPTED_TOOL_GROUPS_HEADER: "gadgets",
                },
            )
            # A runner's own tool calls (sys_agent_download) carry its tunnel token, and a
            # CLI export names the CLI.
            runner_tool = client.get(
                path,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    RUNNER_TUNNEL_TOKEN_HEADER: "synthetic-binding-token",
                },
            )
            cli_export = client.get(
                path,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    "User-Agent": "omnigent-cli/0.0.0 python-httpx/0.0.0",
                },
            )
        assert exported.status_code == 200, exported.text
        assert exported.content == bundle
        assert current_runner.content == bundle
        assert tool_groups(older_runner.content) is None
        assert older_host.status_code == 200, older_host.text
        assert tool_groups(older_host.content) is None
        assert current_host.content == bundle
        assert runner_tool.content == bundle
        assert cli_export.content == bundle

        async def download_via_tool() -> dict[str, object]:
            # Named the way the runner's own server client names itself.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={
                    "X-Forwarded-Email": "author@example.com",
                    "User-Agent": "omnigent-runner/0.0.0 python-httpx/0.0.0",
                },
            ) as server_client:
                result = await _execute_agent_tool(
                    "sys_agent_download",
                    {"session_id": conversation.id},
                    server_client=server_client,
                    agent_spec=None,
                    conversation_id=conversation.id,
                    runner_workspace=tmp_path / "runner",
                )
            return json.loads(result)

        downloaded = asyncio.run(download_via_tool())
        assert tool_groups(Path(str(downloaded["path"])).read_bytes()) == {"gadgets": False}
    finally:
        clear_engine_cache()
