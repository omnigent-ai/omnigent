"""Save-time permission checks for automations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import omnigent.runtime.agent_cache as agent_cache_module
from omnigent.entities.agent import Agent
from omnigent.errors import OmnigentError
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.scheduled.harness_settings import (
    _scheduled_agent_executor,
    validate_scheduled_permission_mode,
    validate_scheduled_permission_support,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore
from tests._helpers.session import bundle_files

_NULL_HINT = "JSON null (not the string 'null') for the agent default"


class _AgentCache:
    def __init__(self, harness: str, permission_mode: str | None) -> None:
        config: dict[str, str] = {"harness": harness}
        if permission_mode is not None:
            config["permission_mode"] = permission_mode
        self._executor = SimpleNamespace(type=harness, config=config)

    def load(
        self, agent_id: str, bundle_location: str, *, expand_env: bool = False
    ) -> SimpleNamespace:
        return SimpleNamespace(spec=SimpleNamespace(executor=self._executor))


_AGENT = SimpleNamespace(id="ag_1", bundle_location="ag_1/hash", operator_authored=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-sdk", "acp", "cursor"])
async def test_sdk_agent_without_a_spec_mode_can_be_saved(harness: str) -> None:
    """SDK harnesses run in auto when the spec sets no mode."""
    await validate_scheduled_permission_support(
        permission_mode=None, agent=_AGENT, agent_cache=_AgentCache(harness, None)
    )


@pytest.mark.asyncio
async def test_sdk_agent_whose_spec_bypasses_approval_cannot_be_saved() -> None:
    with pytest.raises(OmnigentError, match="bypassPermissions"):
        await validate_scheduled_permission_support(
            permission_mode=None,
            agent=_AGENT,
            agent_cache=_AgentCache("claude-sdk", "bypassPermissions"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "mode"),
    [("claude-native", "bypassPermissions"), ("codex-native", "full-access")],
)
async def test_explicit_approval_skipping_mode_can_be_saved(harness: str, mode: str) -> None:
    """A mode the task selects itself is shown in its settings, so it may skip approval."""
    await validate_scheduled_permission_support(
        permission_mode=mode, agent=_AGENT, agent_cache=_AgentCache(harness, None)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-sdk", "acp"])
async def test_sdk_agent_rejects_any_mode_and_asks_for_null(harness: str) -> None:
    """SDK agents take their mode from the spec, so the rejection must say to send null."""
    with pytest.raises(OmnigentError) as raised:
        await validate_scheduled_permission_support(
            permission_mode="auto", agent=_AGENT, agent_cache=_AgentCache(harness, None)
        )
    message = str(raised.value)
    assert f"{harness!r} has no automation permission modes" in message
    assert _NULL_HINT in message


@pytest.mark.asyncio
async def test_native_agent_rejection_lists_its_own_modes() -> None:
    with pytest.raises(OmnigentError) as raised:
        await validate_scheduled_permission_support(
            permission_mode="acceptEdits",
            agent=_AGENT,
            agent_cache=_AgentCache("codex-native", None),
        )
    codex_modes = ", ".join(map(repr, PERMISSION_LAUNCH_ARGS["codex-native"]))
    assert f"Use one of {codex_modes}, or {_NULL_HINT}" in str(raised.value)


@pytest.mark.parametrize("mode", ["null", "unknown-permission-mode"])
def test_unknown_mode_rejection_lists_every_vocabulary(mode: str) -> None:
    with pytest.raises(OmnigentError) as raised:
        validate_scheduled_permission_mode(mode)
    message = str(raised.value)
    for harness, modes in PERMISSION_LAUNCH_ARGS.items():
        assert f"{harness}: {', '.join(map(repr, modes))}" in message
    assert message.index(_NULL_HINT) < message.index("claude-sdk") < message.index("claude-native")


_CUSTOM_HANDLER_CONFIG = yaml.dump(
    {
        "spec_version": 1,
        "name": "custom-handler-agent",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        "guardrails": {
            "policies": {
                "custom_gate": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {"path": "example_org.policies.custom_gate", "arguments": {}},
                }
            }
        },
    }
)


def _store_custom_handler_bundle(store: LocalArtifactStore, location: str) -> None:
    store.put(location, bundle_files({"config.yaml": _CUSTOM_HANDLER_CONFIG.encode()}))


@pytest.fixture()
def real_agent_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentCache:
    # Single-user mode is the test-suite default; disable it so the registry check applies.
    monkeypatch.setattr(agent_cache_module, "local_single_user_enabled", lambda: False)
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    _store_custom_handler_bundle(store, "ag_custom/h1")
    return AgentCache(artifact_store=store, cache_dir=tmp_path / "cache")


@pytest.mark.asyncio
async def test_operator_agent_with_custom_handler_loads_for_scheduled_harness(
    real_agent_cache: AgentCache,
) -> None:
    """An operator-authored agent keeps its custom handler on the scheduled harness path."""
    agent = Agent(
        id="ag_custom",
        created_at=0,
        name="custom-handler-agent",
        bundle_location="ag_custom/h1",
        kind="server",
    )
    assert agent.operator_authored

    harness, executor = await _scheduled_agent_executor(agent, real_agent_cache)

    assert harness == "claude-sdk"
    assert executor is not None


@pytest.mark.asyncio
async def test_tenant_agent_with_custom_handler_is_not_loaded_for_scheduled_harness(
    real_agent_cache: AgentCache,
) -> None:
    """A tenant (user) agent with an unregistered handler gets no executor from this path."""
    agent = Agent(
        id="ag_custom",
        created_at=0,
        name="custom-handler-agent",
        bundle_location="ag_custom/h1",
        kind="user",
        created_by="someone@example.com",
    )
    assert not agent.operator_authored

    assert await _scheduled_agent_executor(agent, real_agent_cache) == (None, None)
