"""Permission settings for automations and the sessions their runs start."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from omnigent.server.scheduled.harness_settings import (
    automation_session_launch_options,
    validate_scheduled_permission_mode,
    validate_scheduled_permission_support,
)
from omnigent.stores.conversation_store import CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY

_NULL_HINT = "JSON null (not the string 'null') for the agent default"


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


class _TaskStore:
    def __init__(self, permission_mode: str | None, user_id: str | None = "alice") -> None:
        self._task = SimpleNamespace(id="task_1", user_id=user_id, permission_mode=permission_mode)

    def get(self, scheduled_task_id: str) -> SimpleNamespace | None:
        return self._task if scheduled_task_id == self._task.id else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "mode", "expected"),
    [
        (
            "codex-native",
            "approve-for-me",
            (PERMISSION_LAUNCH_ARGS["codex-native"]["approve-for-me"], {}),
        ),
        ("claude-native", "auto", (["--permission-mode", "auto"], {})),
        ("codex-native", "bypass", (None, {CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "1"})),
        ("codex-native", "default", (None, {})),
    ],
)
async def test_session_started_by_an_automation_run_uses_its_mode(
    harness: str, mode: str, expected: tuple[list[str] | None, dict[str, str]]
) -> None:
    options = await automation_session_launch_options(
        _TaskStore(mode), "task_1", user_id="alice", harness=harness
    )
    assert options == expected


@pytest.mark.asyncio
async def test_single_user_automation_applies_to_the_local_user() -> None:
    options = await automation_session_launch_options(
        _TaskStore("auto", user_id=None), "task_1", user_id=None, harness="claude-native"
    )
    assert options == (["--permission-mode", "auto"], {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "task_id", "user_id", "harness"),
    [
        pytest.param(None, "task_1", "alice", "codex-native", id="no-task-store"),
        pytest.param(_TaskStore("approve-for-me"), None, "alice", "codex-native", id="not-a-run"),
        pytest.param(_TaskStore("approve-for-me"), "gone", "alice", "codex-native", id="deleted"),
        pytest.param(_TaskStore(None), "task_1", "alice", "codex-native", id="no-mode"),
        pytest.param(
            _TaskStore("approve-for-me"), "task_1", "bob", "codex-native", id="other-owner"
        ),
        pytest.param(
            _TaskStore("approve-for-me"), "task_1", "alice", "claude-native", id="unsupported"
        ),
        pytest.param(_TaskStore("approve-for-me"), "task_1", "alice", None, id="no-harness"),
    ],
)
async def test_session_keeps_its_agent_default_without_an_applicable_mode(
    store: _TaskStore | None, task_id: str | None, user_id: str, harness: str | None
) -> None:
    options = await automation_session_launch_options(
        store, task_id, user_id=user_id, harness=harness
    )
    assert options is None
