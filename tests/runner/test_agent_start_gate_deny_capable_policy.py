"""Tool denials must not block session init or discard sandbox transforms."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.policies import FunctionPolicy
from omnigent.runner import app as runner_app_module
from omnigent.runner import create_runner_app
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
