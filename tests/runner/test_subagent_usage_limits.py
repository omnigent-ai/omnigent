"""Runner subscription usage-limit preemption and mid-fanout spin-down tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import httpx
import pytest

from omnigent.host import usage_limits
from omnigent.host.usage_limits import AccountUsage, UsageWindow
from omnigent.runner import subagent_work
from omnigent.runner.policy import RunnerToolPolicyGate
from omnigent.runner.tool_dispatch import _send_to_existing_session
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
)

PARENT_ID = "conv_parent_polly"
_T = TypeVar("_T")


def _orchestrator_spec(
    *,
    claude_harness: str = "claude-native",
    threshold_percent: float = 90.0,
) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="polly",
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="claude_code",
                executor=ExecutorSpec(type="omnigent", config={"harness": claude_harness}),
            ),
            AgentSpec(
                spec_version=1,
                name="codex",
                executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
            ),
            AgentSpec(
                spec_version=1,
                name="opencode",
                executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
            ),
        ],
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="subscription_usage_guard",
                    on=None,
                    function=FunctionRef(
                        path="omnigent.inner.nessie.policies.subscription_usage_guard",
                        arguments={
                            "threshold_percent": threshold_percent,
                            "window_kinds": ["session", "weekly"],
                            "dispatch_tools": ["sys_session_send", "sys_session_create"],
                        },
                    ),
                )
            ]
        ),
    )


@pytest.fixture
def _isolated_subagent_work() -> Iterator[None]:
    saved_children = dict(subagent_work._subagent_work_by_child)
    saved_parents = {k: set(v) for k, v in subagent_work._subagent_work_by_parent.items()}
    subagent_work._subagent_work_by_child.clear()
    subagent_work._subagent_work_by_parent.clear()
    try:
        yield
    finally:
        subagent_work._subagent_work_by_child.clear()
        subagent_work._subagent_work_by_child.update(saved_children)
        subagent_work._subagent_work_by_parent.clear()
        subagent_work._subagent_work_by_parent.update(saved_parents)


@pytest.mark.asyncio
async def test_sweep_skips_probe_when_no_active_subscription_workers(
    _isolated_subagent_work: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle, launching, or non-subscription workers never trigger CLI probes."""
    monkeypatch.delenv(usage_limits.USAGE_DISABLED_ENV, raising=False)
    monkeypatch.delenv(usage_limits.USAGE_THRESHOLD_ENV, raising=False)

    # Launching claude_code and running opencode should both be skipped before probing.
    subagent_work.register_subagent_work(
        parent_session_id=PARENT_ID,
        child_session_id="conv_launching_claude",
        agent="claude_code",
        title="task-a",
        work_id="work_1",
    )
    opencode_entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_ID,
        child_session_id="conv_running_opencode",
        agent="opencode",
        title="task-b",
        work_id="work_2",
    )
    opencode_entry.status = "running"

    probe_calls: list[object] = []

    async def fail_if_probed(*args: object, **kwargs: object) -> list[AccountUsage]:
        probe_calls.append((args, kwargs))
        return []

    spec = _orchestrator_spec()
    nudged = await subagent_work.sweep_subagent_usage_limits(
        resolve_parent_spec=lambda _pid: _async_val(spec),
        session_harness_name=lambda _cid: None,
        post_nudge=lambda _entry, _msg: _async_val(True),
        usage_reader=fail_if_probed,
    )
    assert nudged == []
    assert probe_calls == []


@pytest.mark.asyncio
async def test_sweep_nudges_throttled_active_worker_once_and_preserves_work_id(
    _isolated_subagent_work: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active workers on a throttled provider receive one WIP checkpoint nudge."""
    monkeypatch.delenv(usage_limits.USAGE_DISABLED_ENV, raising=False)
    monkeypatch.delenv(usage_limits.USAGE_THRESHOLD_ENV, raising=False)
    monkeypatch.setattr(
        usage_limits,
        "subscription_provider_for_worker",
        lambda _sub_spec, harness, *, sub_agent_name=None: (
            "claude"
            if (harness == "claude-native" or sub_agent_name == "claude_code")
            else ("codex" if (harness == "codex-native" or sub_agent_name == "codex") else None)
        ),
    )

    claude_entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_ID,
        child_session_id="conv_claude_worker",
        agent="claude_code",
        title="auth-refactor",
        work_id="work_claude_1",
    )
    claude_entry.status = "running"

    codex_entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_ID,
        child_session_id="conv_codex_worker",
        agent="codex",
        title="api-cleanup",
        work_id="work_codex_1",
    )
    codex_entry.status = "running"

    reset_at = datetime.now(UTC) + timedelta(hours=3)
    sent_nudges: list[tuple[str, str]] = []
    probed_providers: list[tuple[str, ...]] = []

    async def fake_reader(
        providers: tuple[str, ...],
        *,
        max_age_seconds: float = 60.0,
    ) -> list[AccountUsage]:
        assert max_age_seconds == usage_limits.ACTIVE_MONITOR_MAX_AGE_SECONDS
        probed_providers.append(providers)
        return [
            AccountUsage(
                provider="claude",
                account="default",
                status="available",
                windows=[
                    UsageWindow(
                        kind="session",
                        label="5-hour",
                        percent_used=93.0,
                        resets_at=reset_at,
                        window_minutes=300,
                    )
                ],
                observed_at=datetime.now(UTC),
            ),
            AccountUsage(
                provider="codex",
                account="default",
                status="available",
                windows=[
                    UsageWindow(
                        kind="session",
                        label="5-hour",
                        percent_used=35.0,
                        resets_at=reset_at,
                        window_minutes=300,
                    )
                ],
                observed_at=datetime.now(UTC),
            ),
        ]

    async def record_nudge(entry: subagent_work._SubagentWorkEntry, message: str) -> bool:
        sent_nudges.append((entry.child_session_id, message))
        return True

    spec = _orchestrator_spec()
    first = await subagent_work.sweep_subagent_usage_limits(
        resolve_parent_spec=lambda _pid: _async_val(spec),
        session_harness_name=lambda _cid: None,
        post_nudge=record_nudge,
        usage_reader=fake_reader,
    )
    assert [entry.child_session_id for entry in first] == ["conv_claude_worker"]
    assert claude_entry.usage_spindown_nudged is True
    assert claude_entry.work_id == "work_claude_1"
    assert codex_entry.usage_spindown_nudged is False
    assert len(sent_nudges) == 1
    assert "[System: Subscription usage limit reached" in sent_nudges[0][1]
    assert probed_providers == [("claude", "codex")]

    # Second sweep only probes un-nudged codex worker and does not re-nudge claude.
    second = await subagent_work.sweep_subagent_usage_limits(
        resolve_parent_spec=lambda _pid: _async_val(spec),
        session_harness_name=lambda _cid: None,
        post_nudge=record_nudge,
        usage_reader=fake_reader,
    )
    assert second == []
    assert len(sent_nudges) == 1
    assert probed_providers == [("claude", "codex"), ("codex",)]


@pytest.mark.asyncio
async def test_policy_gate_skips_rewritten_mock_subagent_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewritten openai-agents sub-agents map to None and never probe host CLIs."""
    monkeypatch.delenv(usage_limits.USAGE_DISABLED_ENV, raising=False)
    monkeypatch.delenv(usage_limits.USAGE_THRESHOLD_ENV, raising=False)

    probed: list[object] = []

    async def fail_probe(*args: object, **kwargs: object) -> list[AccountUsage]:
        probed.append((args, kwargs))
        return []

    monkeypatch.setattr(usage_limits, "read_usage_limits", fail_probe)
    rewritten = _orchestrator_spec(claude_harness="openai-agents")
    gate = RunnerToolPolicyGate.from_spec(rewritten)
    verdict = await gate.evaluate_tool_call(
        "sys_session_send",
        {"agent": "claude_code", "args": {"purpose": "implement", "input": "task"}},
    )
    assert verdict.action == "allow"
    assert probed == []


@pytest.mark.asyncio
async def test_send_to_existing_session_allows_in_flight_steering_but_blocks_idle_continuation(
    _isolated_subagent_work: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-flight worker steering succeeds at >=90% usage while idle continuation is denied."""
    monkeypatch.delenv(usage_limits.USAGE_DISABLED_ENV, raising=False)
    monkeypatch.delenv(usage_limits.USAGE_THRESHOLD_ENV, raising=False)
    monkeypatch.setattr(
        usage_limits,
        "subscription_provider_for_worker",
        lambda _sub_spec, _harness, *, sub_agent_name=None: (
            "claude" if sub_agent_name == "claude_code" else None
        ),
    )

    reset_at = datetime.now(UTC) + timedelta(hours=1)

    async def throttled_claude(
        _providers: object = ("claude",),
        *,
        max_age_seconds: float = 60.0,
    ) -> list[AccountUsage]:
        del max_age_seconds
        return [
            AccountUsage(
                provider="claude",
                account="default",
                status="available",
                windows=[
                    UsageWindow(
                        kind="session",
                        label="5-hour",
                        percent_used=95.0,
                        resets_at=reset_at,
                        window_minutes=300,
                    )
                ],
                observed_at=datetime.now(UTC),
            )
        ]

    monkeypatch.setattr(usage_limits, "read_usage_limits", throttled_claude)

    active_id = "conv_active_claude"
    idle_id = "conv_idle_claude"
    entry = subagent_work.register_subagent_work(
        parent_session_id=PARENT_ID,
        child_session_id=active_id,
        agent="claude_code",
        title="wip-task",
        work_id="work_active_1",
    )
    entry.status = "running"

    posted_events: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{active_id}":
            return httpx.Response(
                200,
                json={
                    "id": active_id,
                    "status": "running",
                    "busy": True,
                    "parent_session_id": PARENT_ID,
                    "sub_agent_name": "claude_code",
                    "harness": "claude-native",
                    "title": "claude_code:wip-task",
                },
            )
        if request.method == "POST" and request.url.path == f"/v1/sessions/{active_id}/events":
            posted_events.append(request.url.path)
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and request.url.path == f"/v1/sessions/{idle_id}":
            return httpx.Response(
                200,
                json={
                    "id": idle_id,
                    "status": "idle",
                    "busy": False,
                    "parent_session_id": PARENT_ID,
                    "sub_agent_name": "claude_code",
                    "harness": "claude-native",
                    "title": "claude_code:done-task",
                },
            )
        return httpx.Response(500, json={"error": "unexpected request"})

    spec = _orchestrator_spec()
    async with httpx.AsyncClient(
        base_url="https://server.example.com",
        transport=httpx.MockTransport(respond),
    ) as client:
        steered = await _send_to_existing_session(
            active_id,
            "Please commit WIP and wrap up.",
            server_client=client,
            conversation_id=PARENT_ID,
            agent_spec=spec,
        )
        assert "Denied by policy" not in steered
        assert posted_events == [f"/v1/sessions/{active_id}/events"]

        denied = await _send_to_existing_session(
            idle_id,
            "Start another task",
            server_client=client,
            conversation_id=PARENT_ID,
            agent_spec=spec,
        )
        assert denied.startswith("Denied by policy 'subscription_usage_guard':")
        assert "95%" in denied


async def _async_val(val: _T) -> _T:
    return val
