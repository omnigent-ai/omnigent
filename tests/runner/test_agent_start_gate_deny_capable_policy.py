"""Tool denials must not block session init or discard sandbox transforms."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.policies import FunctionPolicy
from omnigent.runner import app as runner_app_module
from omnigent.runner import create_runner_app
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.policy import (
    AGENT_START_TOOL,
    AgentStartPolicyError,
    RunnerToolPolicyGate,
    _GatedPolicy,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    Phase,
    PhaseSelector,
)
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

_START_ARGS = {"harness": "claude-sdk", "sandbox": {"type": "none"}}
_SESSION_ID = "conv_start_policy"


def _allowlist() -> FunctionPolicySpec:
    return FunctionPolicySpec(
        name="allowlist",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(
            path="omnigent.policies.builtins.cel.cel_policy",
            arguments={
                "expression": (
                    'event.data.name in ["ToolSearch", "sys_session_send", "sys_read_inbox"]'
                    ' ? {"result": "ALLOW"} : {"result": "DENY"}'
                )
            },
        ),
    )


def _force_bwrap() -> FunctionPolicySpec:
    return FunctionPolicySpec(
        name="force_bwrap",
        on=None,
        function=FunctionRef(
            path="omnigent.policies.builtins.safety.enforce_sandbox",
            arguments={"sandbox_type": "linux_bwrap", "allow_network": False},
        ),
    )


def _unresolvable(phase: Phase = Phase.TOOL_CALL) -> FunctionPolicySpec:
    return FunctionPolicySpec(
        name="unresolvable",
        on=[PhaseSelector(phase=phase)],
        function=FunctionRef(path="omnigent.policies.builtins.does_not_exist"),
    )


def _spec(*policies: FunctionPolicySpec) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="start-policy-agent",
        executor=ExecutorSpec(
            config={"harness": "claude-sdk"},
            model="test-model",
            auth=ApiKeyAuth(api_key="test-key"),
        ),
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
        guardrails=GuardrailsSpec(policies=list(policies)),
    )


@asynccontextmanager
async def _create_session(spec: AgentSpec):
    pm = Mock(spec=HarnessProcessManager)
    app = create_runner_app(
        process_manager=pm,
        spec_resolver=AsyncMock(return_value=spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            response = await client.post(
                "/v1/sessions", json={"session_id": _SESSION_ID, "agent_id": "ag_test"}
            )
            yield response, pm
    finally:
        runner_app_module._session_inboxes_ref.pop(_SESSION_ID, None)


@pytest.mark.parametrize(
    "sandbox_position", [None, 0, 1], ids=["allowlist-only", "sandbox-first", "sandbox-last"]
)
async def test_session_init_preserves_inbox_and_sandbox(sandbox_position: int | None) -> None:
    policies = [_allowlist()]
    if sandbox_position is not None:
        policies.insert(sandbox_position, _force_bwrap())

    async with _create_session(_spec(*policies)) as (response, pm):
        assert response.status_code == 201, response.text
        pm.get_client.assert_awaited_once()
        assert _SESSION_ID in runner_app_module._session_inboxes_ref
        env = pm.get_client.call_args.kwargs["env"]
        sandbox = json.loads(env["HARNESS_CLAUDE_SDK_OS_ENV"])["sandbox"]
        if sandbox_position is None:
            assert sandbox["type"] == "none"
        else:
            assert sandbox["type"] == "linux_bwrap"
            assert sandbox["allow_network"] is False


async def test_start_probe_keeps_real_tool_enforcement() -> None:
    gate = RunnerToolPolicyGate.from_spec(_spec(_allowlist()))
    assert await gate.evaluate_agent_start(_START_ARGS) is None
    assert (
        await gate.evaluate_tool_call("sys_session_send", {"agent": "child"})
    ).action == "allow"
    assert (await gate.evaluate_tool_call("shell", {"command": "ls"})).action == "deny"


async def test_direct_stream_selection_builds_policy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(_force_bwrap())
    build_environment = Mock(return_value={"SELECTED_ENV": "trusted"})
    monkeypatch.setattr(runner_app_module, "_build_spawn_env_from_spec", build_environment)
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=AsyncMock(return_value=spec),
        server_client=NullServerClient(),
    )
    async with _runner_client(app) as client:
        response = await client.post(
            f"/v1/sessions/{_SESSION_ID}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "agent",
                "content": [],
                "harness": "openai-agents",
                "harness_override": "pi",
                "spawn_env": {"SELECTED_ENV": "untrusted"},
            },
        )
    assert response.status_code == 200, response.text
    selected_spec, selected_harness = build_environment.call_args.args
    assert selected_harness == "openai-agents"
    assert selected_spec.os_env.sandbox.type == "linux_bwrap"
    assert selected_spec.os_env.sandbox.allow_network is False
    assert spec.os_env.sandbox.type == "none"
    assert manager.get_client_calls == [
        (_SESSION_ID, "openai-agents", {"SELECTED_ENV": "trusted"})
    ]


@pytest.mark.parametrize(
    "cache_state",
    ["same-harness", "original-harness", "replaced-entry", "background", "native-selection"],
)
async def test_cached_harness_selection_preserves_single_policy_transform(
    monkeypatch: pytest.MonkeyPatch, cache_state: str
) -> None:
    monkeypatch.setattr(runner_app_module, "_session_inboxes_ref", {})
    declared = _spec()
    resolver = AsyncMock(return_value=declared)

    def transform(spec: AgentSpec, harness: str) -> AgentSpec:
        assert spec is declared
        return replace(spec, name=f"{spec.name}-{harness}", instructions=f"Policy for {harness}.")

    gate = AsyncMock(side_effect=transform)
    build_environment = Mock(side_effect=lambda spec, *args, **kwargs: {"POLICY_NAME": spec.name})
    register_routes = Mock(wraps=runner_app_module.register_mcp_routes)
    monkeypatch.setattr(runner_app_module, "_apply_agent_start_gate", gate)
    monkeypatch.setattr(runner_app_module, "_build_spawn_env_from_spec", build_environment)
    monkeypatch.setattr(runner_app_module, "register_mcp_routes", register_routes)
    dispatch_tool = AsyncMock(return_value="mocked tool result")
    monkeypatch.setattr("omnigent.runner.tool_dispatch.dispatch_tool_locally", dispatch_tool)
    schemas = AsyncMock(return_value=McpSchemasResult([], set(), {}))
    monkeypatch.setattr(runner_app_module.ProxyMcpManager, "schemas_for", schemas)
    launch_terminal = AsyncMock(return_value=True)
    monkeypatch.setattr(runner_app_module, "_launch_native_terminal", launch_terminal)
    worker = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "response"}}),
            _sse(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "status": "action_required",
                        "name": "sys_os_read",
                        "call_id": "call",
                        "arguments": "{}",
                    },
                }
            ),
            _sse(
                {
                    "type": "response.completed",
                    "response": {"id": "response", "status": "completed"},
                }
            ),
        ]
    )
    manager = _FakeProcessManager(worker)
    app = create_runner_app(
        process_manager=manager,
        spec_resolver=resolver,
        server_client=NullServerClient(),
    )
    harness = {
        "original-harness": "claude-sdk",
        "native-selection": "opencode-native",
    }.get(cache_state, "pi")
    async with _runner_client(app) as client:
        initialized = await client.post(
            "/v1/sessions",
            json={
                "session_id": _SESSION_ID,
                "agent_id": "agent",
                "session_init": {
                    "protocol_version": 2,
                    "server_version": "0.6.0.dev0",
                    "session_id": _SESSION_ID,
                    "agent_id": "agent",
                    "snapshot": {
                        "created_at": 1,
                        "updated_at": 1,
                        "labels": {},
                        "harness_override": "pi",
                    },
                },
            },
        )
        assert initialized.status_code == 201, initialized.text
        gate.assert_awaited_once_with(declared, "pi")
        cache = register_routes.call_args.kwargs["_session_spec_cache"]
        original_cached = cache[_SESSION_ID]
        if cache_state == "replaced-entry":
            cache[_SESSION_ID] = replace(cache[_SESSION_ID], name="external-cache-entry")
        query = "" if cache_state == "background" else "?stream=true"
        response = await client.post(
            f"/v1/sessions/{_SESSION_ID}/events{query}",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "agent",
                "harness": harness,
                "has_mcp_servers": cache_state == "original-harness",
            },
        )
        if cache_state == "background":
            task = app.state.active_turns.get(_SESSION_ID)
            if task is not None:
                await asyncio.wait_for(task, timeout=5)
    assert response.status_code == (202 if cache_state == "background" else 200), response.text
    assert manager.get_client_calls[-1] == (
        _SESSION_ID,
        harness,
        {"POLICY_NAME": f"{declared.name}-{harness}"},
    )
    selected = build_environment.call_args.args[0]
    assert f"Policy for {harness}." in worker.posted_bodies[-1]["instructions"]
    dispatch_tool.assert_awaited_once()
    assert dispatch_tool.call_args.kwargs["agent_spec"] is selected
    assert dispatch_tool.call_args.kwargs["effective_harness"] == harness
    if cache_state == "original-harness":
        assert schemas.call_args.args[0] is selected
        assert cache[_SESSION_ID] is original_cached
    if cache_state == "native-selection":
        launch_terminal.assert_awaited_once()
        context = launch_terminal.call_args.args[1]
        assert context.agent_spec is selected
        assert context.ensure_comment_relay.keywords["effective_spec_entry"] is selected
        assert launch_terminal.call_args.kwargs["resolve_agent_spec"] is None
    if cache_state in {"same-harness", "background"}:
        gate.assert_awaited_once_with(declared, "pi")
        resolver.assert_awaited_once()
    else:
        assert gate.await_args_list[1].args == (declared, harness)
        assert resolver.await_count >= 2


@pytest.mark.parametrize("action", ["DENY", "ASK"])
async def test_start_probe_drops_non_allow_transform(action: str) -> None:
    def policy(_event):
        return {"result": action, "data": {"name": AGENT_START_TOOL, "arguments": _START_ARGS}}

    gate = RunnerToolPolicyGate(
        [
            _GatedPolicy(
                "non_allow", FunctionPolicy(_force_bwrap(), policy), frozenset([Phase.TOOL_CALL])
            )
        ]
    )
    assert await gate.evaluate_agent_start(_START_ARGS) is None


@pytest.mark.parametrize("malformed", [False, True], ids=["raises", "malformed"])
async def test_start_probe_fails_closed(malformed: bool) -> None:
    def policy(_event):
        if malformed:
            return {"result": "ALLOW", "data": {"sandbox": {"type": "none"}}}
        raise RuntimeError("transform policy bug")

    gate = RunnerToolPolicyGate(
        [
            _GatedPolicy(
                "broken", FunctionPolicy(_force_bwrap(), policy), frozenset([Phase.TOOL_CALL])
            )
        ]
    )
    with pytest.raises(AgentStartPolicyError, match="broken"):
        await gate.evaluate_agent_start(_START_ARGS)


@pytest.mark.parametrize("phase", [Phase.TOOL_CALL, Phase.TOOL_RESULT])
async def test_unresolved_policy_only_blocks_start_in_tool_call_phase(phase: Phase) -> None:
    gate = RunnerToolPolicyGate.from_spec(_spec(_unresolvable(phase)))
    if phase == Phase.TOOL_CALL:
        with pytest.raises(AgentStartPolicyError, match="failed to resolve"):
            await gate.evaluate_agent_start(_START_ARGS)
    else:
        assert await gate.evaluate_agent_start(_START_ARGS) is None

    assert (await gate.evaluate_tool_call("sys_session_send", {})).action == "deny"
    assert "Denied by policy: unresolvable" in await gate.evaluate_tool_result(
        "sys_session_send", ""
    )


async def test_session_init_fails_closed_on_unresolved_policy() -> None:
    async with _create_session(_spec(_unresolvable())) as (response, pm):
        assert response.status_code == 403, response.text
        assert response.json()["error"] == "agent_start_policy_unevaluable"
        pm.get_client.assert_not_awaited()
        assert _SESSION_ID not in runner_app_module._session_inboxes_ref
