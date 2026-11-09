"""Save-time permission checks for automations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.scheduled.harness_settings import validate_scheduled_permission_support


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
