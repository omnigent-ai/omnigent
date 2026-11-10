"""Save-time permission checks for automations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from omnigent.server.scheduled.harness_settings import (
    validate_scheduled_permission_mode,
    validate_scheduled_permission_support,
)


class _AgentCache:
    def __init__(self, harness: str, permission_mode: str | None) -> None:
        config: dict[str, str] = {"harness": harness}
        if permission_mode is not None:
            config["permission_mode"] = permission_mode
        self._executor = SimpleNamespace(type=harness, config=config)

    def load(self, agent_id: str, bundle_location: str) -> SimpleNamespace:
        return SimpleNamespace(spec=SimpleNamespace(executor=self._executor))


_AGENT = SimpleNamespace(id="ag_1", bundle_location="ag_1/hash")


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
async def test_sdk_agent_rejects_a_task_mode_and_says_to_send_null() -> None:
    """claude-sdk has no task modes, so the rejection must name JSON null as the fix."""
    with pytest.raises(OmnigentError) as raised:
        await validate_scheduled_permission_support(
            permission_mode="auto", agent=_AGENT, agent_cache=_AgentCache("claude-sdk", None)
        )
    assert str(raised.value) == (
        "permission_mode 'auto' is not supported for 'claude-sdk': 'claude-sdk' has no task "
        "permission modes; send JSON null (not the string 'null') for the agent default."
    )


@pytest.mark.asyncio
async def test_native_agent_rejection_lists_its_harness_modes() -> None:
    with pytest.raises(OmnigentError) as raised:
        await validate_scheduled_permission_support(
            permission_mode="smart", agent=_AGENT, agent_cache=_AgentCache("claude-native", None)
        )
    assert str(raised.value) == (
        "permission_mode 'smart' is not supported for 'claude-native': 'claude-native' accepts "
        "'default', 'auto', 'acceptEdits', 'plan', 'dontAsk', 'bypassPermissions', "
        "or JSON null (not the string 'null') for the agent default."
    )


@pytest.mark.parametrize("mode", ["null", "unknown-permission-mode"])
def test_unknown_mode_rejection_lists_every_vocabulary(mode: str) -> None:
    with pytest.raises(OmnigentError) as raised:
        validate_scheduled_permission_mode(mode)
    message = str(raised.value)
    assert message.startswith(f"Unsupported permission_mode: {mode!r}. ")
    for harness, modes in PERMISSION_LAUNCH_ARGS.items():
        assert f"{harness}: {', '.join(modes)}" in message
    assert "other harnesses, including claude-sdk, have none" in message
    assert message.endswith("or JSON null (not the string 'null') for the agent default.")
