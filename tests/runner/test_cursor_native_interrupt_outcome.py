"""Cursor interrupts wait for the stop hook's actual turn outcome."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.entities import ConversationItem, MessageData, PagedList
from omnigent.harnesses.cursor_native import bridge
from omnigent.harnesses.cursor_native import forwarder as fwd
from omnigent.harnesses.cursor_native import status as cursor_status
from omnigent.runner import app as runner_app
from omnigent.runner import subagent_work
from omnigent.server.routes._sessions.orchestration import (
    _enrich_terminal_status_with_subagent_output,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.stores.conversation_store import ConversationStore
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.parametrize("append_later", [False, True], ids=["backlog", "during-retry"])
async def test_damaged_stop_marker_recovers_parent_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, append_later: bool
) -> None:
    """A truncated record fails with partial output, then the next dispatch completes."""
    from tests.runner.test_native_subagent_turn_outcome_delivery import _Rig

    marker_file = tmp_path / cursor_status.TURN_END_FILE
    marker_file.write_text('{"status": "abort', encoding="utf-8")
    if not append_later:
        cursor_status.record_turn_end(tmp_path, {"status": "completed"})
    read_outcome = cursor_status.read_turn_outcome
    reads = 0

    def read_then_append(directory: Path, marker: int) -> str:
        nonlocal reads
        reads += 1
        try:
            return read_outcome(directory, marker)
        finally:
            if append_later and reads == 1:
                cursor_status.record_turn_end(tmp_path, {"status": "completed"})

    monkeypatch.setattr(cursor_status, "read_turn_outcome", read_then_append)
    monkeypatch.setattr(fwd, "_discover_store", lambda *a: None)
    rig = _Rig("cursor-native")
    store = Mock(spec=ConversationStore)
    results = []
    failure_posts = 0
    task = None
    try:
        async with _runner_client(rig.app) as client:

            async def forward(request: httpx.Request) -> httpx.Response:
                nonlocal failure_posts
                body = json.loads(request.content)
                if body["data"]["status"] == "failed":
                    failure_posts += 1
                    if failure_posts == 1:
                        return httpx.Response(503)
                reply = "Partial research" if not results else "Next task completed"
                store.list_items.return_value = PagedList(
                    data=[
                        ConversationItem(
                            id="reply",
                            type="message",
                            status="completed",
                            response_id="response",
                            created_at=1,
                            data=MessageData(
                                role="assistant",
                                agent="cursor-native-ui",
                                content=[{"type": "text", "text": reply}],
                            ),
                        )
                    ]
                )
                body["data"] = await _enrich_terminal_status_with_subagent_output(
                    body["data"], body["data"]["status"], rig.child_id, store
                )
                response = await client.post(f"/v1/sessions/{rig.child_id}/events", json=body)
                assert response.status_code == 204, response.text
                results.extend(rig.drained())
                if len(results) == 1:
                    subagent_work.register_subagent_work(
                        parent_session_id=rig.parent_id,
                        child_session_id=rig.child_id,
                        agent="researcher",
                        title="next task",
                    )
                return httpx.Response(204)

            client_type = httpx.AsyncClient
            monkeypatch.setattr(
                fwd.httpx,
                "AsyncClient",
                lambda **kwargs: client_type(
                    transport=httpx.MockTransport(forward),
                    **kwargs,
                ),
            )
            task = asyncio.create_task(
                fwd.forward_cursor_store_to_session(
                    base_url="http://test",
                    headers={},
                    session_id=rig.child_id,
                    bridge_dir=tmp_path,
                    workspace="/ws",
                    launch_epoch_ms=0,
                    agent_name="cursor-native-ui",
                    poll_interval_s=0.01,
                )
            )
            async with asyncio.timeout(2):
                while cursor_status.read_posted_count(tmp_path) != 2:
                    await asyncio.sleep(0.01)
            assert [(r["status"], r["output"]) for r in results] == [
                ("failed", "Partial research"),
                ("completed", "Next task completed"),
            ]
            assert failure_posts == 2
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        rig.close()


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
        subagent_work._session_inboxes_ref[parent_id] = inbox
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="cursor-native",
        title="research",
    )
    subagent_work.register_child_session(
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
                subagent_work._session_inboxes_ref[parent_id] = inbox
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
        subagent_work.unregister_subagent_work_for_session(parent_id)
        subagent_work.unregister_child_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
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
    subagent_work.register_subagent_work(
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
                subagent_work.mark_subagent_work_terminal(
                    child_id, status="cancelled", output="Interrupt requested"
                )
            inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            subagent_work._session_inboxes_ref[parent_id] = inbox
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
        subagent_work.unregister_subagent_work_for_session(parent_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
