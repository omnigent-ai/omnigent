"""Managed model policy is checked before provisioning or persistence."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes.sessions import routes_core, routes_events
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


class _Catalog:
    def __init__(self) -> None:
        self.revision = "revision-1"
        self.owner = "local"
        self.calls: list[tuple[str | None, str, str | None]] = []
        self.runtime_config = {
            "providers": {
                "bifrost": {
                    "kind": "gateway",
                    "openai": {
                        "base_url": "https://gateway.example/v1",
                        "api_key_ref": "env:POD_GATEWAY_KEY",
                        "wire_api": "responses",
                    },
                }
            },
            "inference": {
                "harnesses": {
                    harness: {
                        "provider": "bifrost",
                        "default_model": "gateway/main",
                        "model_allowlist": ["gateway/main", "gateway/fast"],
                    }
                    for harness in ("codex", "acp:custom")
                }
            },
        }

    async def prepare(
        self,
        provider: str | None,
        harness: str,
        user_id: str | None,
        agent_auth: object = None,
    ) -> dict[str, Any]:
        self.calls.append((provider, harness, user_id))
        return {
            "version": 1,
            "target_id": "sandbox:agent_sandbox",
            "configuration_revision": self.revision,
            "harness": harness,
            "owner_id": self.owner,
            "runtime_config": copy.deepcopy(self.runtime_config),
            "model_discovery": {},
            "connections": {},
            "catalog": {
                "configured": True,
                "configuration_revision": self.revision,
                "provider_label": "Bifrost",
                "default_model": "gateway/main",
                "status": "ready",
                "models": [
                    {"id": "gateway/main", "displayName": "Main", "isDefault": True},
                    {"id": "gateway/fast", "displayName": "Fast", "isDefault": False},
                ],
            },
        }

    async def catalog(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(snapshot["catalog"])


@dataclass
class _Env:
    app: FastAPI
    client: AsyncClient
    store: SqlAlchemyConversationStore
    catalog: _Catalog
    launch: AsyncMock
    artifact_dir: Path

    def persisted(self) -> tuple[list[str], list[tuple[str, bytes]]]:
        return (
            sorted(row.id for row in self.store.list_conversations(limit=100, kind=None).data),
            sorted(
                (str(path.relative_to(self.artifact_dir)), path.read_bytes())
                for path in self.artifact_dir.rglob("*")
                if path.is_file()
            ),
        )


@pytest_asyncio.fixture
async def env(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Env]:
    artifact_dir = tmp_path / "artifacts"
    artifact_store = LocalArtifactStore(str(artifact_dir))
    catalog = _Catalog()
    config = ManagedSandboxConfig(
        provider="agent_sandbox",
        server_url="http://test",
        launcher_factory=lambda: None,
        token_ttl_s=100,
        host_config=catalog.runtime_config,
    )
    store = SqlAlchemyConversationStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
        sandbox_config=ManagedSandboxDeployment.single(config),
    )
    app.state.inference_catalog = catalog
    launch = AsyncMock()
    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield _Env(app, client, store, catalog, launch, artifact_dir)


async def _json_create(env: _Env, **overrides: Any):
    agent = await create_test_agent(
        env.client,
        executor={"type": "omnigent", "config": {"harness": "codex"}},
        include_llm=False,
    )
    return await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "managed",
            "sandbox_provider": "agent_sandbox",
            "inference_configuration_revision": "revision-1",
            **overrides,
        },
    )


@pytest.mark.parametrize("location", ["provider", "discovery"])
async def test_literal_credentials_rejected_before_session_or_artifact_writes(env: _Env, location):
    from omnigent.server.inference_catalog import SandboxInferenceService

    target = env.app.state.sandbox_config.default
    if location == "provider":
        target.host_config["providers"]["bifrost"]["openai"]["api_key_ref"] = "literal-test-token"
    else:
        target.model_discovery["bifrost"] = {
            "base_url": "https://catalog.example/v1",
            "api_key_ref": "literal-test-token",
        }
    env.app.state.inference_catalog = SandboxInferenceService(env.app.state)
    agent = await create_test_agent(
        env.client,
        executor={"type": "omnigent", "config": {"harness": "codex"}},
        include_llm=False,
    )
    before = env.persisted()
    response = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "managed",
            "sandbox_provider": "agent_sandbox",
        },
    )
    assert response.status_code == 400, response.text
    assert "api_key_ref" in response.text
    assert "literal-test-token" not in response.text
    assert env.persisted() == before
    env.launch.assert_not_awaited()


async def _multipart_create(env: _Env, *, executor: dict[str, Any] | None = None, **metadata: Any):
    return await env.client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps(
                {
                    "host_type": "managed",
                    "sandbox_provider": "agent_sandbox",
                    "inference_configuration_revision": "revision-1",
                    **metadata,
                }
            )
        },
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(
                    "managed-codex",
                    executor=executor or {"type": "omnigent", "config": {"harness": "codex"}},
                    include_llm=False,
                ),
                "application/gzip",
            )
        },
    )


async def test_preview_preserves_acp_slug_without_creating_session_or_sandbox(env: _Env):
    before = env.persisted()
    response = await env.client.get(
        "/v1/sandbox-providers/agent_sandbox/harnesses/acp:custom/model-options"
    )
    assert response.status_code == 200, response.text
    assert response.json()["configuration_revision"] == "revision-1"
    assert [row["id"] for row in response.json()["models"]] == ["gateway/main", "gateway/fast"]
    assert env.catalog.calls == [("agent_sandbox", "acp:custom", None)]
    assert env.persisted() == before
    env.launch.assert_not_called()


@pytest.mark.parametrize(
    "harness,family,agent_harness",
    [
        ("claude-native", "anthropic", "claude-native"),
        ("codex-native", "openai", "codex-native"),
        ("pi-native", "openai", "pi-native"),
        ("pi-native", "openai", "native-pi"),
        ("opencode-native", "openai", "native-opencode"),
    ],
)
@pytest.mark.parametrize("discovery", [False, True])
async def test_native_alias_preview_and_create_use_canonical_harness(
    env: _Env, harness, family, agent_harness, discovery, monkeypatch: pytest.MonkeyPatch
):
    from omnigent.server.inference_catalog import SandboxInferenceService

    target = env.app.state.sandbox_config.default
    config = target.host_config
    alias = "native-" + harness.removesuffix("-native")
    config["inference"]["harnesses"] = {alias: {"provider": "bifrost"}}
    provider = config["providers"]["bifrost"]
    provider[family] = provider.pop("openai")
    if harness == "opencode-native":
        provider[family]["wire_api"] = "chat"
    monkeypatch.setenv("ALIAS_DISCOVERY_KEY", "test-catalog-key")
    if discovery:
        target.model_discovery["bifrost"] = {
            "base_url": "https://catalog.example/v1",
            "api_key_ref": "env:ALIAS_DISCOVERY_KEY",
        }
    else:
        config["inference"]["harnesses"][alias]["model_allowlist"] = ["gateway/main"]
    env.app.state.inference_catalog = SandboxInferenceService(
        env.app.state,
        transport=MockTransport(
            lambda request: Response(200, json={"data": [{"id": "gateway/main"}]})
        ),
    )
    agent = await create_test_agent(
        env.client,
        executor={"type": "omnigent", "config": {"harness": agent_harness}},
        include_llm=False,
    )
    preview = await env.client.get(
        f"/v1/sandbox-providers/agent_sandbox/harnesses/{harness}/model-options",
        params={"agent_id": agent["id"]},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "ready", preview.text
    created = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "managed",
            "sandbox_provider": "agent_sandbox",
            "inference_configuration_revision": preview.json()["configuration_revision"],
        },
    )
    assert created.status_code == 201, created.text
    saved = env.store.get_conversation(created.json()["id"])
    assert saved is not None
    assert saved.model_override == "gateway/main"
    assert saved.inference_snapshot["harness"] == harness
    assert (
        saved.inference_snapshot["configuration_revision"]
        == preview.json()["configuration_revision"]
    )
    assert (
        saved.inference_snapshot["runtime_config"]["inference"]["harnesses"][alias][
            "default_model"
        ]
        == "gateway/main"
    )


@pytest.mark.parametrize("selected", [None, "gateway/fast"])
async def test_json_create_captures_selected_model_and_configuration(env: _Env, selected):
    response = await _json_create(env, model_override=selected)
    assert response.status_code == 201, response.text
    saved = env.store.get_conversation(response.json()["id"])
    assert saved is not None
    assert saved.model_override == (selected or "gateway/main")
    assert saved.inference_snapshot is not None
    assert saved.inference_snapshot["configuration_revision"] == "revision-1"
    assert saved.inference_snapshot["runtime_config"] == env.catalog.runtime_config
    await asyncio.sleep(0)
    env.launch.assert_awaited_once()


@pytest.mark.parametrize("multipart", [False, True])
async def test_external_create_on_configured_server_does_not_capture_sandbox_policy(
    env, multipart
):
    create = _multipart_create if multipart else _json_create
    response = await create(
        env,
        host_type="external",
        sandbox_provider=None,
        inference_configuration_revision=None,
        **({} if multipart else {"model_override": "gpt-legacy"}),
    )
    assert response.status_code == 201, response.text
    saved = env.store.get_conversation(response.json()["session_id" if multipart else "id"])
    assert saved.inference_snapshot is None
    assert saved.model_override == (None if multipart else "gpt-legacy")
    assert env.catalog.calls == []
    env.launch.assert_not_awaited()


@pytest.mark.parametrize("explicit_policy", [True, False])
async def test_multipart_create_captures_default_model_in_saved_configuration(
    env: _Env, explicit_policy: bool
):
    if not explicit_policy:
        binding = env.catalog.runtime_config["inference"]["harnesses"]["codex"]
        del binding["default_model"]
        del binding["model_allowlist"]
    response = await _multipart_create(env)
    assert response.status_code == 201, response.text
    saved = env.store.get_conversation(response.json()["session_id"])
    assert saved is not None and saved.inference_snapshot is not None
    snapshot = saved.inference_snapshot
    assert snapshot["configuration_revision"] == "revision-1"
    assert snapshot["catalog"]["default_model"] == "gateway/main"
    assert saved.model_override == snapshot["catalog"]["default_model"]
    assert snapshot["runtime_config"] == env.catalog.runtime_config
    await asyncio.sleep(0)
    env.launch.assert_awaited_once()


@pytest.mark.parametrize("multipart", [False, True])
async def test_stale_revision_rejects_before_rows_artifacts_or_provisioning(env: _Env, multipart):
    agent = await create_test_agent(
        env.client,
        executor={"type": "omnigent", "config": {"harness": "codex"}},
        include_llm=False,
    )
    env.catalog.revision = "revision-2"
    before = env.persisted()
    if multipart:
        response = await _multipart_create(env)
    else:
        response = await env.client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_type": "managed",
                "sandbox_provider": "agent_sandbox",
                "inference_configuration_revision": "revision-1",
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "inference_configuration_changed"
    assert env.persisted() == before
    env.launch.assert_not_called()


async def test_unlisted_patch_rejects_before_any_metadata_change(env: _Env):
    created = await _json_create(env, model_override="gateway/main", title="Original")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    before = env.store.get_conversation(session_id)
    response = await env.client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "model_override": "gateway/hidden",
            "title": "Must not apply",
            "labels": {"review": "must-not-apply"},
            "reasoning_effort": "high",
        },
    )
    assert response.status_code == 400, response.text
    assert "configured model list" in response.text
    after = env.store.get_conversation(session_id)
    assert after is not None and before is not None
    assert after.title == before.title
    assert after.labels == before.labels
    assert after.model_override == before.model_override
    assert after.reasoning_effort == before.reasoning_effort
    assert after.inference_snapshot == before.inference_snapshot


@pytest.mark.parametrize(
    "previous_model,selection,forwarded_model",
    [
        ("gateway/main", "gateway/fast", "gateway/fast"),
        (None, "gateway/fast", "gateway/fast"),
        ("gateway/fast", "default", "gateway/main"),
    ],
)
async def test_rejected_native_switch_restores_saved_model(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    previous_model: str | None,
    selection: str,
    forwarded_model: str,
):
    """An allowed selection must not persist when the live native runner rejects it."""
    bindings = env.catalog.runtime_config["inference"]["harnesses"]
    bindings["codex-native"] = copy.deepcopy(bindings["codex"])
    agent = await create_test_agent(
        env.client,
        name="codex-native-ui",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        include_llm=False,
    )
    created = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "managed",
            "sandbox_provider": "agent_sandbox",
            "model_override": previous_model,
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    if previous_model is None:
        env.store.update_conversation(session_id, _unset_model_override=True)
    original = env.store.get_conversation(session_id)
    assert original is not None
    assert original.model_override == previous_model
    runner_post = AsyncMock(
        return_value=Response(
            503,
            json={"detail": "The terminal did not confirm the model switch"},
            request=Request("POST", f"/v1/sessions/{session_id}/events"),
        )
    )
    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client",
        AsyncMock(return_value=SimpleNamespace(post=runner_post)),
    )

    response = await env.client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": selection}
    )

    assert response.status_code == 503, response.text
    assert "previous selection has been restored" in response.text
    runner_post.assert_awaited_once()
    assert runner_post.call_args.args == (f"/v1/sessions/{session_id}/events",)
    assert runner_post.call_args.kwargs["json"] == {
        "type": "model_change",
        "model": forwarded_model,
    }
    saved = env.store.get_conversation(session_id)
    assert saved is not None
    assert saved.model_override == previous_model
    assert saved.inference_snapshot == original.inference_snapshot


async def test_inherited_child_rejects_conflicting_auth_before_persistence(env: _Env):
    created = await _json_create(env)
    assert created.status_code == 201, created.text
    before = env.persisted()
    prepare_calls = len(env.catalog.calls)
    response = await env.client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": created.json()["id"]})},
        files={
            "bundle": (
                "child.tar.gz",
                build_agent_bundle(
                    "child",
                    executor={
                        "type": "omnigent",
                        "config": {"harness": "codex"},
                        "auth": {"type": "provider", "name": "other"},
                    },
                    include_llm=False,
                ),
                "application/gzip",
            )
        },
    )
    assert response.status_code == 400, response.text
    assert "conflict" in response.text.lower()
    assert len(env.catalog.calls) == prepare_calls
    assert env.persisted() == before


async def test_default_reset_and_catalog_use_saved_config_after_operator_edit(env: _Env):
    created = await _json_create(env, model_override="gateway/fast")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    env.catalog.revision = "revision-2"
    binding = env.catalog.runtime_config["inference"]["harnesses"]["codex"]
    binding["default_model"] = "gateway/fast"
    binding["model_allowlist"] = ["gateway/fast"]
    calls = len(env.catalog.calls)
    response = await env.client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": "default", "silent": True}
    )
    assert response.status_code == 200, response.text
    saved = env.store.get_conversation(session_id)
    assert saved is not None
    assert saved.model_override == "gateway/main"
    snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["inference_configured"] is True
    assert snapshot.json()["inference_error"] is None
    assert [row["id"] for row in snapshot.json()["model_options"]] == [
        "gateway/main",
        "gateway/fast",
    ]
    assert len(env.catalog.calls) == calls


@pytest.mark.parametrize("event_type", ["message", "slash_command"])
async def test_unavailable_saved_provider_rejects_input_before_dispatch_or_items(
    env: _Env, monkeypatch: pytest.MonkeyPatch, event_type: str
):
    created = await _json_create(env)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    unavailable = {
        "configured": True,
        "configuration_revision": "revision-1",
        "status": "unavailable",
        "models": [],
        "default_model": "gateway/main",
        "provider_label": "Bifrost",
        "error": "The configured provider is unavailable.",
    }
    monkeypatch.setattr(env.catalog, "catalog", AsyncMock(return_value=unavailable))
    dispatch = AsyncMock()
    slash_dispatch = AsyncMock()
    monkeypatch.setattr(routes_events, "_dispatch_session_event_to_runner", dispatch)
    monkeypatch.setattr(routes_events, "_dispatch_skill_slash_command_to_runner", slash_dispatch)
    before = env.store.list_items(session_id).data
    data = (
        {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}
        if event_type == "message"
        else {"agent": "codex", "name": "review", "arguments": ""}
    )
    response = await env.client.post(
        f"/v1/sessions/{session_id}/events", json={"type": event_type, "data": data}
    )
    assert response.status_code == 400, response.text
    assert "configured provider is unavailable" in response.text
    assert env.store.list_items(session_id).data == before
    dispatch.assert_not_called()
    slash_dispatch.assert_not_called()
    snapshot = await env.client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["inference_configured"] is True
    assert snapshot.json()["model_options"] == []
    assert snapshot.json()["inference_error"] == unavailable["error"]


@pytest.mark.parametrize("runner_status", [409, 200])
@pytest.mark.parametrize("previous_runner", [None, "runner_previous"])
async def test_configured_rebind_rejection_restores_previous_runner(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    runner_status: int,
    previous_runner: str | None,
):
    created = await _json_create(env, title="Original")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    if previous_runner is not None:
        env.store.replace_runner_id(session_id, previous_runner)
    runner_post = AsyncMock(
        return_value=Response(
            runner_status,
            json={"error": "configuration rejected"} if runner_status == 409 else {},
            request=Request("POST", "/v1/sessions"),
        )
    )
    monkeypatch.setattr(
        sessions_module,
        "_registered_runner_id",
        lambda _router, runner_id, **_kwargs: runner_id,
    )
    monkeypatch.setattr(
        routes_core,
        "_get_runner_client",
        AsyncMock(return_value=SimpleNamespace(post=runner_post)),
    )
    relay = AsyncMock()
    monkeypatch.setattr(routes_core, "_ensure_runner_relay_ready", relay)
    response = await env.client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": "runner_replacement", "title": "Must not apply"},
    )
    assert response.status_code == 503, response.text
    assert "did not accept" in response.text
    saved = env.store.get_conversation(session_id)
    assert saved is not None
    assert saved.runner_id == previous_runner
    assert saved.title == "Original"
    runner_post.assert_awaited_once()
    init = runner_post.call_args.kwargs["json"]["session_init"]["snapshot"]
    assert init["inference_config"] == saved.inference_snapshot["runtime_config"]
    relay.assert_not_called()


async def _builtin(env: _Env, harness: str) -> str:
    agent = await create_test_agent(
        env.client,
        name=f"target-{harness}",
        executor={"type": "omnigent", "config": {"harness": harness}},
        include_llm=False,
    )
    stored = env.app.state.agent_store.get(agent["id"])
    assert stored is not None
    builtin = env.app.state.agent_store.create(
        generate_agent_id(), f"builtin-{harness}", stored.bundle_location
    )
    return builtin.id


@pytest.mark.parametrize("source_harness", ["pi-native", "native-pi"])
async def test_saved_profile_fork_accepts_an_alias_for_the_same_harness(env: _Env, source_harness):
    env.catalog.runtime_config["inference"]["harnesses"][source_harness] = {
        "provider": "bifrost",
        "default_model": "gateway/main",
        "model_allowlist": ["gateway/main"],
    }
    source_agent_id = await _builtin(env, source_harness)
    target_agent_id = await _builtin(
        env, "native-pi" if source_harness == "pi-native" else "pi-native"
    )
    snapshot = await env.catalog.prepare("agent_sandbox", source_harness, "local")
    source = env.store.create_conversation(agent_id=source_agent_id, inference_snapshot=snapshot)
    response = await env.client.post(
        f"/v1/sessions/{source.id}/fork", json={"agent_id": target_agent_id}
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("unbound_harness", [False, True])
async def test_configured_session_rejects_agent_switch_before_mutation(
    env: _Env, unbound_harness: bool
):
    if unbound_harness:
        del env.catalog.runtime_config["inference"]["harnesses"]["codex"]
    created = await _json_create(env, model_override="gateway/fast")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    target_id = await _builtin(env, "claude-sdk")
    before = env.persisted()
    original = env.store.get_conversation(session_id)
    response = await env.client.post(
        f"/v1/sessions/{session_id}/switch-agent", json={"agent_id": target_id}
    )
    assert response.status_code == 400, response.text
    assert "Start a new session" in response.text
    assert env.persisted() == before
    saved = env.store.get_conversation(session_id)
    assert saved is not None and original is not None
    assert saved.agent_id == original.agent_id
    assert saved.model_override == original.model_override
    assert saved.inference_snapshot == original.inference_snapshot
    assert env.app.state.agent_store.get(original.agent_id) is not None


@pytest.mark.parametrize("unbound_harness", [False, True])
async def test_configured_fork_rejects_different_harness_before_persistence(
    env: _Env, unbound_harness: bool
):
    if unbound_harness:
        del env.catalog.runtime_config["inference"]["harnesses"]["codex"]
    created = await _json_create(env)
    assert created.status_code == 201, created.text
    target_id = await _builtin(env, "claude-sdk")
    before = env.persisted()
    response = await env.client.post(
        f"/v1/sessions/{created.json()['id']}/fork", json={"agent_id": target_id}
    )
    assert response.status_code == 400, response.text
    assert "same harness" in response.text
    assert env.persisted() == before


@pytest.mark.parametrize("unbound_harness", [False, True])
async def test_configured_fork_rejects_foreign_provider_owner(env: _Env, unbound_harness: bool):
    if unbound_harness:
        del env.catalog.runtime_config["inference"]["harnesses"]["codex"]
    env.catalog.owner = "alice@example.com"
    created = await _json_create(env)
    assert created.status_code == 201, created.text
    before = env.persisted()
    response = await env.client.post(f"/v1/sessions/{created.json()['id']}/fork", json={})
    assert response.status_code == 400, response.text
    assert "another user" in response.text
    assert env.persisted() == before


@pytest.mark.parametrize(
    "selection,expected",
    [
        ({}, "gateway/fast"),
        ({"model_override": "gateway/main"}, "gateway/main"),
        ({"model_override": "default"}, "gateway/main"),
        ({"model_override": None}, "gateway/main"),
        ({"model_override": "gateway/hidden"}, None),
    ],
)
async def test_same_harness_fork_validates_model_against_saved_policy(
    env: _Env, selection: dict[str, Any], expected: str | None
):
    created = await _json_create(env, model_override="gateway/fast")
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    before = env.persisted()
    original = env.store.get_conversation(session_id)
    response = await env.client.post(f"/v1/sessions/{session_id}/fork", json=selection)
    if expected is None:
        assert response.status_code == 400, response.text
        assert "configured model list" in response.text
        assert env.persisted() == before
    else:
        assert response.status_code == 201, response.text
        fork = env.store.get_conversation(response.json()["id"])
        assert fork is not None and original is not None
        assert fork.model_override == expected
        assert fork.inference_snapshot == original.inference_snapshot


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("provider", ["kubernetes", "agent_sandbox"])
@pytest.mark.parametrize("harness", ["claude-native", "codex", "pi-native", "acp:custom"])
@pytest.mark.parametrize("pin", [None, "gateway/fast", "obsolete-agent-model"])
async def test_create_validates_agent_pin_before_side_effects(
    env, multipart, provider, harness, pin
):
    env.catalog.runtime_config["inference"]["harnesses"][harness] = {
        "provider": "bifrost",
        "default_model": "gateway/main",
        "model_allowlist": ["gateway/main", "gateway/fast"],
    }
    env.app.state.sandbox_config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            provider=provider,
            server_url="http://test",
            launcher_factory=lambda: None,
            token_ttl_s=100,
            host_config=env.catalog.runtime_config,
        )
    )
    executor = {"type": "omnigent", "config": {"harness": harness}, "model": pin}
    agent = await create_test_agent(env.client, executor=executor, include_llm=False)
    before = env.persisted()
    if multipart:
        response = await _multipart_create(env, executor=executor, sandbox_provider=provider)
    else:
        response = await env.client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_type": "managed",
                "sandbox_provider": provider,
                "model_override": "gateway/main",
                "inference_configuration_revision": "revision-1",
            },
        )
    if pin == "obsolete-agent-model":
        assert response.status_code == 400, response.text
        assert pin in response.text and harness in response.text
        assert "Remove executor.model" in response.text
        assert env.persisted() == before
        env.launch.assert_not_called()
    else:
        assert response.status_code == 201, response.text
        saved = env.store.get_conversation(response.json()["session_id" if multipart else "id"])
        assert saved.model_override == "gateway/main"


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("switching_agent", [False, True])
async def test_fork_and_child_pin_use_saved_policy_before_writes(env, child, switching_agent):
    agent = await create_test_agent(
        env.client,
        executor={
            "type": "omnigent",
            "config": {"harness": "codex"},
            "model": "obsolete-agent-model",
        },
        include_llm=False,
    )
    stored = env.app.state.agent_store.get(agent["id"])
    target = env.app.state.agent_store.create(
        generate_agent_id(), "pinned-builtin", stored.bundle_location
    )
    source_agent = await _builtin(env, "codex") if switching_agent else target.id
    snapshot = await env.catalog.prepare("agent_sandbox", "codex", "local")
    source = env.store.create_conversation(
        agent_id=source_agent,
        inference_snapshot=snapshot,
    )
    env.catalog.runtime_config["inference"]["harnesses"]["codex"]["model_allowlist"].append(
        "obsolete-agent-model"
    )
    before = env.persisted()
    if child:
        response = await _multipart_create(
            env,
            parent_session_id=source.id,
            executor={
                "type": "omnigent",
                "config": {"harness": "codex"},
                "model": "obsolete-agent-model",
            },
        )
    else:
        response = await env.client.post(
            f"/v1/sessions/{source.id}/fork",
            json={
                **({"agent_id": target.id} if switching_agent else {}),
                "model_override": "gateway/main",
            },
        )
    assert response.status_code == 400, response.text
    assert "obsolete-agent-model" in response.text
    assert env.persisted() == before
    env.launch.assert_not_called()


@pytest.mark.parametrize("mode", ["ordinary-host", "unconfigured-target", "unbound-harness"])
async def test_conflicting_pin_is_not_restricted_without_sandbox_binding(env, mode):
    agent = await create_test_agent(
        env.client,
        executor={
            "type": "omnigent",
            "config": {"harness": "codex"},
            "model": "obsolete-agent-model",
        },
        include_llm=False,
    )
    if mode == "unconfigured-target":
        env.catalog.runtime_config["inference"]["harnesses"].clear()
    elif mode == "unbound-harness":
        del env.catalog.runtime_config["inference"]["harnesses"]["codex"]
    response = await env.client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "model_override": "gateway/main",
            **(
                {"host_type": "managed", "sandbox_provider": "agent_sandbox"}
                if mode != "ordinary-host"
                else {}
            ),
        },
    )
    assert response.status_code == 201, response.text
    assert env.store.get_conversation(response.json()["id"]).model_override == "gateway/main"
