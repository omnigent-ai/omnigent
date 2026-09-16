"""Session ownership and harness transport for disposable sandbox writes."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, WritePathSpec
from omnigent.runner.resource_registry import SessionResourceRegistry


@pytest.fixture
def spec(tmp_path):
    return SimpleNamespace(
        os_env=OSEnvSpec(
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="linux_bwrap", write_paths=[WritePathSpec(".", True)]),
        )
    )


def test_filesystem_panel_default_is_upgraded_before_cow_tools(spec, tmp_path, monkeypatch):
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    default = Mock(sandbox=SimpleNamespace(copy_on_write_roots=None))
    shared = Mock(sandbox=SimpleNamespace(copy_on_write_roots=[tmp_path]))
    factory = Mock(side_effect=[default, shared])
    monkeypatch.setattr(registry, "_create_primary_env", factory)
    assert registry._resolve_primary("session", None) is default
    assert registry._resolve_primary("session", spec) is shared
    default.close.assert_called_once()
    assert registry._resolve_primary("session", spec) is shared
    assert registry._resolve_primary("session", None) is shared
    assert factory.call_count == 2


def test_session_cannot_silently_change_copy_on_write_configuration(spec, tmp_path, monkeypatch):
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    shared = Mock(sandbox=SimpleNamespace(copy_on_write_roots=[tmp_path]))
    monkeypatch.setattr(registry, "_create_primary_env", Mock(return_value=shared))
    registry._resolve_primary("session", spec)
    changed = SimpleNamespace(os_env=replace(spec.os_env, sandbox=OSEnvSandboxSpec(type="none")))
    with pytest.raises(ValueError, match="new session"):
        registry._resolve_primary("session", changed)
    shared.close.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_consumers_close_before_owner(spec, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    closed = []
    terminals = Mock()
    terminals.cleanup_conversation = AsyncMock(side_effect=lambda *a: closed.append("terminals"))
    registry = SessionResourceRegistry(terminal_registry=terminals, runner_workspace=tmp_path)
    shared = Mock()
    shared.close.side_effect = lambda: closed.append("owner")
    monkeypatch.setattr(registry, "_create_primary_env", Mock(return_value=shared))
    registry._resolve_primary("session", spec)
    await registry.cleanup_session("session")
    assert closed == ["terminals", "owner"]
    assert "session" not in registry._primary_env_specs


def test_harness_spawn_transports_prepared_session_namespace(spec, tmp_path, monkeypatch):
    from omnigent.inner.sandbox import SandboxPolicy
    from omnigent.runner.app import _build_spawn_env_from_spec
    from omnigent.sandbox.copy_on_write import SHARED_ENVIRONMENT_VAR, attach_shared_environment
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    agent = AgentSpec(
        spec_version=1,
        name="cow-test",
        os_env=spec.os_env,
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_openai_agents_sdk_spawn_env", lambda spec: {}
    )
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[tmp_path],
        write_files=[],
        allow_network=False,
        copy_on_write_roots=[tmp_path],
    )
    environment = Mock(sandbox=policy)
    environment.prepare_sandbox.side_effect = lambda p: setattr(
        p, "copy_on_write_namespace", (1, 2, 3)
    )
    registry = Mock()
    registry.resolve_environment.return_value = environment
    env = _build_spawn_env_from_spec(
        agent, "openai-agents", session_id="session", resource_registry=registry
    )
    assert env is not None
    monkeypatch.setenv(SHARED_ENVIRONMENT_VAR, env[SHARED_ENVIRONMENT_VAR])
    borrowed = replace(policy, copy_on_write_namespace=None)
    attach_shared_environment(borrowed)
    assert borrowed.copy_on_write_namespace == (1, 2, 3)
    environment.prepare_sandbox.assert_called_once_with(policy)
    with pytest.raises(ValueError, match="resource registry"):
        _build_spawn_env_from_spec(agent, "openai-agents", session_id="session")


@pytest.mark.parametrize("cwd", [None, "", ".", "./"])
@pytest.mark.parametrize("terminal_first", [False, True])
def test_terminal_materialization_keeps_same_environment(
    spec, tmp_path, monkeypatch, cwd, terminal_first
):
    from omnigent.tools.builtins.sys_terminal import _synthesize_parent_os_env

    agent = SimpleNamespace(os_env=replace(spec.os_env, cwd=cwd))
    parent = _synthesize_parent_os_env(agent.os_env, str(tmp_path))
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    shared = Mock(sandbox=SimpleNamespace(copy_on_write_roots=[tmp_path]))
    factory = Mock(return_value=shared)
    monkeypatch.setattr(registry, "_create_primary_env", factory)
    if terminal_first:
        assert registry._resolve_terminal_environment("session", parent) is shared
    assert registry._resolve_primary("session", agent) is shared
    assert registry._resolve_terminal_environment("session", parent) is shared
    factory.assert_called_once()


@pytest.mark.parametrize("harness", ["codex", "codex-native", "claude-sdk", "pi", "unknown"])
def test_unsupported_harness_rejected_before_spawn(spec, harness):
    from omnigent.runner.app import _build_spawn_env_from_spec
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    agent = AgentSpec(
        spec_version=1,
        name="cow",
        os_env=spec.os_env,
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    with pytest.raises(ValueError, match="openai-agents"):
        _build_spawn_env_from_spec(agent, harness)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("upload_file", {"path": "original"}),
        ("sys_agent_download", {"session_id": "session"}),
        ("sys_agent_list", {}),
        ("load_skill", {"name": "example"}),
        ("read_skill_file", {"path": "example"}),
        ("sys_session_create", {"config_path": "agent.yaml"}),
    ],
)
async def test_host_filesystem_tools_rejected_in_cow_session(spec, name, args):
    import json
    from unittest.mock import AsyncMock

    from omnigent.runner.tool_dispatch import execute_tool

    client = AsyncMock()
    result = await execute_tool(
        tool_name=name,
        arguments=json.dumps(args),
        agent_spec=spec,
        conversation_id="session",
        server_client=client,
    )
    assert "does not yet support copy_on_write" in result
    client.get.assert_not_called()
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_cached_cow_still_rejects_host_io_when_spec_unavailable(spec, tmp_path, monkeypatch):
    from omnigent.runner.tool_dispatch import execute_tool

    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    shared = Mock(sandbox=SimpleNamespace(copy_on_write_roots=[tmp_path]))
    monkeypatch.setattr(registry, "_create_primary_env", Mock(return_value=shared))
    registry._resolve_primary("session", spec)
    result = await execute_tool(
        tool_name="upload_file",
        arguments='{"path":"original"}',
        agent_spec=None,
        resource_registry=registry,
        conversation_id="session",
    )
    assert "does not yet support copy_on_write" in result


@pytest.mark.asyncio
async def test_stream_harness_selection_preserves_cow_restriction(spec):
    from omnigent.runner import create_runner_app
    from omnigent.spec.types import AgentSpec, ExecutorSpec
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
    from tests.runner.helpers import NullServerClient

    agent = AgentSpec(
        spec_version=1,
        name="cow",
        os_env=spec.os_env,
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )

    async def resolver(agent_id, session_id=None):
        return agent

    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=manager, spec_resolver=resolver, server_client=NullServerClient()
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions/cow-stream/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "harness": "codex",
                "agent_id": "agent",
                "content": [{"type": "input_text", "text": "Read the workspace"}],
            },
        )
    assert response.status_code == 400, response.text
    assert "openai-agents" in response.text
    assert not manager.get_client_calls


@pytest.mark.asyncio
async def test_background_turn_registers_tools_from_shared_environment(
    spec, tmp_path, monkeypatch
):
    import asyncio

    from omnigent.inner.sandbox import SandboxPolicy
    from omnigent.runner import create_runner_app
    from omnigent.spec.types import AgentSpec, ExecutorSpec
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
    from tests.runner.helpers import NullServerClient

    agent = AgentSpec(
        spec_version=1,
        name="cow",
        os_env=replace(spec.os_env, cwd="."),
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[tmp_path],
        write_files=[],
        allow_network=False,
        copy_on_write_roots=[tmp_path],
        copy_on_write_namespace=(1, 2, 3),
    )
    environment = Mock(sandbox=policy)
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    monkeypatch.setattr(registry, "_create_primary_env", Mock(return_value=environment))
    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_openai_agents_sdk_spawn_env", lambda spec: {}
    )
    monkeypatch.setattr(
        "omnigent.inner.os_env.create_os_environment",
        Mock(side_effect=ValueError("unresolved cwd")),
    )

    async def resolver(agent_id, session_id=None):
        return agent

    finished = asyncio.Event()
    harness = _ScriptedHarnessClient([], stream_finished=finished)
    manager = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=NullServerClient(),
        resource_registry=registry,
        runner_workspace=tmp_path,
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions/cow-schema/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "agent",
                "content": [{"type": "input_text", "text": "Read the workspace"}],
            },
        )
        assert response.status_code == 202, response.text
        await asyncio.wait_for(finished.wait(), timeout=5)
    tools = harness.posted_bodies[0]["tools"]
    names = {tool.get("name") or tool.get("function", {}).get("name") for tool in tools}
    assert {"sys_os_read", "sys_os_write", "sys_os_shell"} <= names
