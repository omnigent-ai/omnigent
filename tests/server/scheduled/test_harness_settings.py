"""Permission settings for automations and the sessions their runs start."""

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
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.scheduled.harness_settings import (
    _scheduled_agent_executor,
    automation_session_launch_options,
    creator_session_launch_options,
    forget_unrecorded_legacy_run_session,
    note_unrecorded_legacy_run_session,
    session_permission_mode,
    validate_scheduled_permission_mode,
    validate_scheduled_permission_support,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY
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
async def test_a_continued_task_without_a_mode_wakes_the_session_in_its_own() -> None:
    """Continuing a session inherits nothing from the spec: the session keeps its mode."""
    await validate_scheduled_permission_support(
        permission_mode=None,
        agent=_AGENT,
        agent_cache=_AgentCache("claude-sdk", "bypassPermissions"),
        continues_session=True,
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


_RUN_SESSION = "conv_run"
_KEPT_SESSION = "conv_kept"


class _TaskStore:
    """One task, ``task_1``, whose run is starting or running in ``conv_run``."""

    def __init__(
        self,
        permission_mode: str | None,
        user_id: str | None = "alice",
        *,
        state: str = "active",
        kept_session_id: str | None = None,
        running: dict[str, str] | None = None,
    ) -> None:
        self._task = SimpleNamespace(
            id="task_1",
            user_id=user_id,
            permission_mode=permission_mode,
            state=state,
            session_id=kept_session_id,
            continues_session=kept_session_id is not None,
        )
        # conversation id -> task id of the run running in it
        self._running = {_RUN_SESSION: "task_1"} if running is None else running

    def get(self, scheduled_task_id: str) -> SimpleNamespace | None:
        return self._task if scheduled_task_id == self._task.id else None

    def has_active_run(self, scheduled_task_id: str, conversation_id: str) -> bool:
        return self._running.get(conversation_id) == scheduled_task_id


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
        _TaskStore(mode),
        "task_1",
        source_session_id=_RUN_SESSION,
        user_id="alice",
        harness=harness,
    )
    assert options == expected


@pytest.mark.asyncio
async def test_continue_automation_kept_session_uses_its_mode() -> None:
    """A continue task's run is finished once its prompt is delivered, so its kept
    session counts without a running run."""
    store = _TaskStore("auto", kept_session_id=_KEPT_SESSION, running={})
    options = await automation_session_launch_options(
        store, "task_1", source_session_id=_KEPT_SESSION, user_id="alice", harness="claude-native"
    )
    assert options == (["--permission-mode", "auto"], {})


@pytest.mark.asyncio
async def test_legacy_fire_session_counts_before_its_run_row_exists() -> None:
    """A legacy fire sends its prompt before writing the run row, so its session is
    noted in between."""
    store = _TaskStore("auto", running={})
    note_unrecorded_legacy_run_session("conv_legacy", "task_1")
    try:
        options = await automation_session_launch_options(
            store,
            "task_1",
            source_session_id="conv_legacy",
            user_id="alice",
            harness="claude-native",
        )
    finally:
        forget_unrecorded_legacy_run_session("conv_legacy")
    assert options == (["--permission-mode", "auto"], {})
    assert (
        await automation_session_launch_options(
            store,
            "task_1",
            source_session_id="conv_legacy",
            user_id="alice",
            harness="claude-native",
        )
        is None
    )


@pytest.mark.asyncio
async def test_single_user_automation_applies_to_the_local_user() -> None:
    """Single-user automations are ownerless, and their callers are ``local``."""
    options = await automation_session_launch_options(
        _TaskStore("auto", user_id=None),
        "task_1",
        source_session_id=_RUN_SESSION,
        user_id=RESERVED_USER_LOCAL,
        harness="claude-native",
    )
    assert options == (["--permission-mode", "auto"], {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "task_id", "source", "user_id", "harness"),
    [
        pytest.param(None, "task_1", _RUN_SESSION, "alice", "codex-native", id="no-task-store"),
        pytest.param(
            _TaskStore("approve-for-me"),
            None,
            _RUN_SESSION,
            "alice",
            "codex-native",
            id="not-a-run",
        ),
        pytest.param(
            _TaskStore("approve-for-me"),
            "gone",
            _RUN_SESSION,
            "alice",
            "codex-native",
            id="unknown",
        ),
        pytest.param(
            _TaskStore("approve-for-me", state="deleted"),
            "task_1",
            _RUN_SESSION,
            "alice",
            "codex-native",
            id="soft-deleted",
        ),
        pytest.param(
            _TaskStore("approve-for-me"), "task_1", "conv_fork", "alice", "codex-native", id="fork"
        ),
        pytest.param(
            _TaskStore("approve-for-me", running={}),
            "task_1",
            _RUN_SESSION,
            "alice",
            "codex-native",
            id="run-finished",
        ),
        pytest.param(
            _TaskStore("approve-for-me", running={_RUN_SESSION: "task_2"}),
            "task_1",
            _RUN_SESSION,
            "alice",
            "codex-native",
            id="another-task-run",
        ),
        pytest.param(
            _TaskStore("approve-for-me"), "task_1", None, "alice", "codex-native", id="no-source"
        ),
        pytest.param(
            _TaskStore(None), "task_1", _RUN_SESSION, "alice", "codex-native", id="no-mode"
        ),
        pytest.param(
            _TaskStore("approve-for-me"),
            "task_1",
            _RUN_SESSION,
            "bob",
            "codex-native",
            id="other-owner",
        ),
        pytest.param(
            _TaskStore("approve-for-me"),
            "task_1",
            _RUN_SESSION,
            "alice",
            "claude-native",
            id="unsupported",
        ),
        pytest.param(
            _TaskStore("approve-for-me"), "task_1", _RUN_SESSION, "alice", None, id="no-harness"
        ),
    ],
)
async def test_session_keeps_its_agent_default_without_an_applicable_mode(
    store: _TaskStore | None,
    task_id: str | None,
    source: str | None,
    user_id: str,
    harness: str | None,
) -> None:
    options = await automation_session_launch_options(
        store, task_id, source_session_id=source, user_id=user_id, harness=harness
    )
    assert options is None


_CODEX_APPROVAL_MODE = "omnigent.codex_native.approval_mode"
_CLAUDE_PERMISSION_MODE = "omnigent.claude_native.permission_mode"
_CODEX_ARGS = PERMISSION_LAUNCH_ARGS["codex-native"]
_CODEX_FULL_ACCESS = (_CODEX_ARGS["full-access"], {})


@pytest.mark.parametrize(
    ("harness", "labels", "launch_args", "on_creator_host", "expected"),
    [
        pytest.param("codex-native", {}, _CODEX_ARGS["full-access"], True, _CODEX_FULL_ACCESS),
        # Codex resumes with the same settings however the args spell them.
        pytest.param(
            "codex-native",
            {},
            ["-s", "danger-full-access", "-a", "never", "-c", 'approvals_reviewer="user"'],
            True,
            _CODEX_FULL_ACCESS,
            id="codex-spelling",
        ),
        pytest.param(
            "codex-native",
            {},
            ["--dangerously-bypass-approvals-and-sandbox"],
            True,
            _CODEX_FULL_ACCESS,
            id="codex-bypass-flag",
        ),
        # No preset matches, so none is widened to fit.
        pytest.param(
            "codex-native",
            {},
            ["--ask-for-approval", "never", "--sandbox", "workspace-write"],
            True,
            None,
            id="codex-no-preset",
        ),
        pytest.param(
            "codex-native",
            {},
            ["-c", 'default_permissions=":danger-full-access"'],
            True,
            _CODEX_FULL_ACCESS,
            id="codex-full-access-profile",
        ),
        pytest.param(
            "codex-native",
            {},
            ["-c", 'default_permissions="team"', "-c", 'approval_policy="never"'],
            True,
            None,
            id="codex-custom-profile",
        ),
        pytest.param(
            "codex-native",
            {},
            [*_CODEX_ARGS["approve-for-me"], "-c", "sandbox_workspace_write.network_access=true"],
            True,
            None,
            id="codex-custom-config",
        ),
        # Text after ``--`` is a prompt, never an option.
        pytest.param(
            "codex-native",
            {},
            ["--", "--dangerously-bypass-approvals-and-sandbox"],
            True,
            None,
            id="codex-prompt-text",
        ),
        pytest.param(
            "codex-native", {}, ["--", "--approve-for-me"], True, None, id="codex-prompt-text-mode"
        ),
        # A file profile can set more than a preset, so none stands in for it...
        pytest.param(
            "codex-native",
            {},
            ["--profile", "restricted", *_CODEX_ARGS["full-access"]],
            True,
            None,
            id="codex-file-profile",
        ),
        # ...though a reported mode that keeps approval still applies.
        pytest.param(
            "codex-native",
            {_CODEX_APPROVAL_MODE: "approve-for-me"},
            ["-p", "restricted", *_CODEX_ARGS["approve-for-me"]],
            True,
            (_CODEX_ARGS["approve-for-me"], {}),
            id="codex-file-profile-reported-mode",
        ),
        pytest.param(
            "claude-native",
            {},
            ["--", "--permission-mode", "bypassPermissions"],
            True,
            None,
            id="claude-prompt-text",
        ),
        # Only Claude Code's and Codex's launch settings are read.
        pytest.param("cursor-native", {}, ["--yolo"], True, None, id="other-harness"),
        pytest.param("pi", {}, None, True, None, id="no-permission-modes"),
        pytest.param(
            "claude-native",
            {},
            ["--permission-mode", "plan", "--permission-mode=acceptEdits"],
            False,
            (["--permission-mode", "acceptEdits"], {}),
            id="claude-last-flag",
        ),
        pytest.param(
            "claude-native",
            {},
            ["--dangerously-skip-permissions"],
            True,
            (["--permission-mode", "bypassPermissions"], {}),
            id="claude-skip-flag",
        ),
        # A switch through the web picker updates only the reported mode.
        pytest.param(
            "codex-native",
            {_CODEX_APPROVAL_MODE: "approve-for-me"},
            _CODEX_ARGS["full-access"],
            True,
            (_CODEX_ARGS["approve-for-me"], {}),
            id="reported-mode",
        ),
        pytest.param(
            "codex-native",
            {CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "1", _CODEX_APPROVAL_MODE: "full-access"},
            None,
            True,
            (None, {CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "1"}),
            id="bypass",
        ),
        # A bypass launch switched to a mode that asks keeps asking.
        pytest.param(
            "codex-native",
            {CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "1", _CODEX_APPROVAL_MODE: "ask-for-approval"},
            None,
            True,
            (_CODEX_ARGS["ask-for-approval"], {}),
            id="bypass-switched",
        ),
        # Editors can write the reported mode, so it never skips approval.
        pytest.param(
            "codex-native",
            {_CODEX_APPROVAL_MODE: "full-access"},
            _CODEX_ARGS["approve-for-me"],
            True,
            (_CODEX_ARGS["approve-for-me"], {}),
            id="reported-approval-skipping-mode",
        ),
        pytest.param(
            "claude-native",
            {_CLAUDE_PERMISSION_MODE: "bypassPermissions"},
            None,
            True,
            None,
            id="reported-claude-bypass",
        ),
        pytest.param(
            "claude-native",
            {_CLAUDE_PERMISSION_MODE: "auto"},
            None,
            False,
            (["--permission-mode", "auto"], {}),
            id="claude-off-host",
        ),
        # A mode that skips approval was chosen for the creator's host only.
        pytest.param(
            "codex-native",
            {_CODEX_APPROVAL_MODE: "full-access"},
            _CODEX_ARGS["full-access"],
            False,
            None,
            id="approval-skipping-off-host",
        ),
        pytest.param(
            "claude-native",
            {},
            ["--permission-mode", "bypassPermissions"],
            False,
            None,
            id="claude-bypass-off-host",
        ),
        pytest.param(
            "codex-native",
            {CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "1"},
            None,
            False,
            None,
            id="bypass-off-host",
        ),
        pytest.param("codex-native", {}, None, True, None, id="mode-unknown"),
    ],
)
def test_session_another_session_starts_uses_the_creators_mode(
    harness: str,
    labels: dict[str, str],
    launch_args: list[str] | None,
    on_creator_host: bool,
    expected: tuple[list[str] | None, dict[str, str]] | None,
) -> None:
    creator = SimpleNamespace(labels=labels, terminal_launch_args=launch_args)
    options = creator_session_launch_options(
        creator,
        harness=harness,
        creator_harness=harness,
        on_creator_host=on_creator_host,
    )
    assert options == expected


@pytest.mark.parametrize(
    ("harness", "creator_harness"),
    [("claude-native", "codex-native"), ("codex-native", None), (None, "codex-native")],
)
def test_creator_mode_needs_the_same_harness(
    harness: str | None, creator_harness: str | None
) -> None:
    """Each harness has its own vocabulary, so a mode never carries across harnesses."""
    creator = SimpleNamespace(
        labels={_CODEX_APPROVAL_MODE: "approve-for-me"},
        terminal_launch_args=_CODEX_ARGS["approve-for-me"],
    )
    options = creator_session_launch_options(
        creator, harness=harness, creator_harness=creator_harness, on_creator_host=True
    )
    assert options is None


@pytest.mark.parametrize(
    ("harness", "launch_args", "expected"),
    [
        ("cursor-native", ["--yolo"], "yolo"),
        ("devin-native", ["--permission-mode", "smart"], "smart"),
        # Text after ``--`` is the prompt, not an option.
        ("cursor-native", ["--", "--yolo"], None),
    ],
)
def test_other_harnesses_report_their_launch_mode_without_passing_it_on(
    harness: str, launch_args: list[str], expected: str | None
) -> None:
    """Only Claude Code's and Codex's launch settings pass a mode to a session they start."""
    session = SimpleNamespace(labels={}, terminal_launch_args=launch_args)
    assert session_permission_mode(session, harness) == expected
    options = creator_session_launch_options(
        session, harness=harness, creator_harness=harness, on_creator_host=True
    )
    assert options is None


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
