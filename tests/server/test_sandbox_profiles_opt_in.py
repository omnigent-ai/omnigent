"""Default deployments must not depend on the profile schema or provider hooks."""

from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.server.sandbox_profile_launch import prepare_profile
from omnigent.server.schemas import SessionCreateRequest
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore


@pytest.mark.asyncio
@pytest.mark.parametrize("features", [None, "sandbox_profiles,-sandbox_profiles"])
async def test_disabled_profiles_do_not_read_catalog_or_wrap_admission(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: str | None,
) -> None:
    from omnigent.server import sandbox_profiles

    catalog = Mock(side_effect=AssertionError("Profile catalog must remain inactive"))
    monkeypatch.setattr(sandbox_profiles, "SandboxProfileCatalog", catalog)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    hosts = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        host_store=hosts,
        sandbox_config=parse_sandbox_config(
            {"provider": "kubernetes", "server_url": "https://test.example.com"}
        ),
        feature_flags=resolve_feature_flags(
            {} if features is None else {"OMNIGENT_FEATURES": features}
        ),
    )
    assert app.state.sandbox_profiles is None
    catalog.assert_not_called()
    assert "sandbox_profile_bindings" not in inspect(hosts._engine).get_table_names()
    host_id = uuid4().hex
    hosts.upsert_on_connect(host_id, "existing-host", "local")
    conversation = conversations.create_conversation(host_id=host_id, workspace="/tmp")
    app.state.host_registry.launch_authorizer(host_id, conversation.id, None, None, False, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        info = await client.get("/v1/info")
        assert info.status_code == 200
        assert info.json()["features"]["sandbox_profiles"] is False
        factory = Mock(
            side_effect=AssertionError("Profile preflight must not construct providers")
        )
        monkeypatch.setattr(app.state.sandbox_config.default, "launcher_factory", factory)
        assert (
            await prepare_profile(
                Request({"type": "http", "app": app}),
                SessionCreateRequest(agent_id=uuid4().hex, host_type="managed"),
            )
            is None
        )
        factory.assert_not_called()
        listing = await client.get("/v1/sandbox-profiles")
        assert listing.status_code == 200
        assert listing.json()["profiles"] == []
        write = await client.put(
            "/v1/sandbox-profiles/build",
            json={
                "name": "Build",
                "provider": "kubernetes",
                "config": {},
            },
        )
        assert write.status_code in (404, 405)
    assert hosts.delete_host(host_id) is not None
    assert conversations.get_conversation(conversation.id).host_id is None
