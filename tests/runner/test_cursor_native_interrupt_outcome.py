"""Cursor interrupts wait for the stop hook's actual turn outcome."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.cursor_native import bridge
from omnigent.runner import app as runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.parametrize(
    ("outcome", "session_status", "output", "parent_ready"),
    [
        ("completed", "idle", "VERDICT: 3 citations, all check out.", True),
        ("cancelled", "idle", "Partial research: checked one citation.", True),
        ("failed", "failed", "Provider failed before research finished.", True),
        ("cancelled", "idle", "Partial research: checked one citation.", False),
    ],
)
async def test_cursor_interrupt_delivers_actual_stop_outcome(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    session_status: str,
    output: str,
    parent_ready: bool,
) -> None:
    """Ignored Escape preserves success; an actual abort preserves cancellation."""
    parent_id, child_id = uuid.uuid4().hex, uuid.uuid4().hex
    injected: list[object] = []
    monkeypatch.setattr(bridge, "inject_interrupt", lambda path, **kw: injected.append(path))
    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_cursor_terminal", AsyncMock()
    )

    async def resolve_spec(*_args: Any, **_kwargs: Any) -> AgentSpec:
        return AgentSpec(
            spec_version=1,
            name="research",
            executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
        )

    app = runner_app.create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        spec_resolver=resolve_spec,
    )
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    if parent_ready:
        runner_app._session_inboxes_ref[parent_id] = inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="cursor-native",
        title="research",
    )
    runner_app.register_child_session(
        child_id, parent_session_id=parent_id, title="research", tool="cursor", session_name="r"
    )
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": child_id, "agent_id": uuid.uuid4().hex}
            )
            assert created.status_code == 201, created.text
            assert child_id not in app.state.active_turns
            endpoint = f"/v1/sessions/{child_id}/events"
            interrupted = await client.post(endpoint, json={"type": "interrupt"})
            assert interrupted.status_code == 204, interrupted.text
            assert len(injected) == 1
            premature_result = not inbox.empty()
            terminal = await client.post(
                endpoint,
                json={
                    "type": "external_session_status",
                    "data": {
                        "status": session_status,
                        "turn_outcome": outcome,
                        "output": output,
                    },
                },
            )
            assert terminal.status_code == (204 if parent_ready else 503), terminal.text
            assert not premature_result, (
                "Escape requested cancellation before its outcome was known"
            )
            if not parent_ready:
                bare_idle = {"type": "external_session_status", "data": {"status": "idle"}}
                retry = await client.post(endpoint, json=bare_idle)
                assert retry.status_code == 503, retry.text
                runner_app._session_inboxes_ref[parent_id] = inbox
                retry = await client.post(endpoint, json=bare_idle)
                assert retry.status_code == 204, retry.text
            result = inbox.get_nowait()
            assert (result["status"], result["output"]) == (outcome, output)
            assert inbox.empty()
            await client.post(
                endpoint, json={"type": "external_session_status", "data": {"status": "idle"}}
            )
            events = _drain_session_event_queue(runner_app._session_event_queues_ref[parent_id])
            child_updates = [e for e in events if e.get("type") == "session.child_session.updated"]
            assert child_updates[-1]["child"]["current_task_status"] == outcome
    finally:
        runner_app.unregister_subagent_work_for_session(parent_id)
        runner_app.unregister_child_session(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)


@pytest.mark.parametrize("confirmed", [True, False])
async def test_idle_retry_preserves_only_confirmed_cancellation_without_cached_spec(
    confirmed: bool,
) -> None:
    """Recovered native outcomes survive retries; optimistic interrupts remain correctable."""
    parent_id, child_id = uuid.uuid4().hex, uuid.uuid4().hex
    app = runner_app.create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id, child_session_id=child_id, agent="worker", title="research"
    )
    try:
        async with _runner_client(app) as client:
            endpoint = f"/v1/sessions/{child_id}/events"
            if confirmed:
                terminal = await client.post(
                    endpoint,
                    json={
                        "type": "external_session_status",
                        "data": {
                            "status": "idle",
                            "turn_outcome": "cancelled",
                            "output": "Partial research",
                        },
                    },
                )
                assert terminal.status_code == 503, terminal.text
            else:
                runner_app.mark_subagent_work_terminal(
                    child_id, status="cancelled", output="Interrupt requested"
                )
            inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            runner_app._session_inboxes_ref[parent_id] = inbox
            retried = await client.post(
                endpoint,
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "Later result"},
                },
            )
            assert retried.status_code == 204, retried.text
            result = inbox.get_nowait()
            expected = (
                ("cancelled", "Partial research") if confirmed else ("completed", "Later result")
            )
            assert (result["status"], result["output"]) == expected
    finally:
        runner_app.unregister_subagent_work_for_session(parent_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
