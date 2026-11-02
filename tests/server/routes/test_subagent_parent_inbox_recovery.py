"""Cold-parent delivery recovery must not bypass lifecycle or runner ownership."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.server.routes import sessions
from omnigent.server.runner_session_init import RunnerSessionInitializer


@pytest.fixture
async def recovery(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    parent = Conversation(
        id="parent",
        created_at=1,
        updated_at=1,
        root_conversation_id="parent",
        agent_id="agent",
        runner_id="runner",
        live_status="failed",
        labels={"omnigent.last_task_error_code": "provider_error"},
    )
    child = Conversation(
        id="child",
        created_at=1,
        updated_at=1,
        root_conversation_id="parent",
        agent_id="agent",
        runner_id="runner",
        kind="sub_agent",
        parent_conversation_id="parent",
    )
    rows = {parent.id: parent, child.id: child}
    owners = {parent.id: "owner", child.id: None}
    state = SimpleNamespace(
        rows=rows,
        owners=owners,
        parent=parent,
        child=child,
        owner="owner",
        generation=1,
        init_status=201,
        delivery_status=204,
        requests=[],
        after_init=None,
        after_relay=None,
        after_delivery=None,
        read_error=False,
    )

    def get_conversations(ids: list[str]) -> dict[str, Conversation]:
        if state.read_error:
            raise ConnectionError("store temporarily unavailable")
        return {key: rows[key] for key in ids if key in rows}

    state.store = SimpleNamespace(
        get_conversations=get_conversations,
        get_session_owner=lambda sid, **_: owners.get(sid),
    )
    state.registry = SimpleNamespace(get=lambda _: state)
    state.initializer = RunnerSessionInitializer(state.registry, server_version="test")

    async def respond(request: httpx.Request) -> httpx.Response:
        state.requests.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/v1/sessions":
            if state.after_init:
                state.after_init()
            return httpx.Response(state.init_status, json={})
        assert request.url.path == "/v1/sessions/child/events"
        if state.after_delivery:
            state.after_delivery()
        return httpx.Response(state.delivery_status)

    async def relay(*_args: Any, **_kwargs: Any) -> None:
        if state.after_relay:
            state.after_relay()

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", relay)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        state.client = client
        state.router = SimpleNamespace(
            runner_owner=lambda _: state.owner,
            client_for_session_resources=lambda *_a, **_k: SimpleNamespace(client=client),
        )
        state.rejection = sessions._RunnerForwardResult(
            status_code=503,
            body=json.dumps(
                {"error": "subagent_delivery_not_confirmed", "reason": "missing_parent_inbox"}
            ),
        )
        state.body = {
            "type": "external_session_status",
            "data": {"status": "idle", "output": "done"},
        }
        yield state


async def recover(state: Any) -> Any:
    return await sessions._recover_subagent_missing_parent_inbox(
        state.child, state.rejection, state.router, state.store, state.initializer, state.body
    )


@pytest.mark.parametrize(
    "response",
    [
        None,
        (204, ""),
        (503, "unavailable"),
        (503, "[]"),
        (503, '{"error":"other","reason":"missing_parent_inbox"}'),
        (503, '{"error":"subagent_delivery_not_confirmed","reason":"missing_work_entry"}'),
    ],
)
async def test_other_rejections_do_not_initialize_parent(recovery: Any, response: Any) -> None:
    recovery.rejection = None if response is None else sessions._RunnerForwardResult(*response)
    assert await recover(recovery) is None
    assert recovery.requests == []


@pytest.mark.parametrize("phase", ["before_init", "after_init", "after_relay", "after_delivery"])
@pytest.mark.parametrize(
    "guard",
    [
        "archived",
        "closed",
        "stopped",
        "interrupted",
        "nested",
        "parent_rebound",
        "child_rebound",
        "reparented",
        "owner_mismatch",
        "child_owner_mismatch",
        "owner_unknown",
        "missing_parent",
        "missing_child",
        "removed_agent",
        "unreadable",
    ],
)
async def test_parent_recovery_preserves_lifecycle_and_ownership(
    recovery: Any,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    guard: str,
) -> None:
    def change() -> None:
        parent, child = recovery.parent, recovery.child
        if guard == "archived":
            recovery.rows[parent.id] = replace(parent, archived=True)
        elif guard == "closed":
            recovery.rows[parent.id] = replace(parent, labels={"omnigent.closed": "true"})
        elif guard == "stopped":
            monkeypatch.setitem(sessions._intentional_stop_sessions, parent.id, "runner")
        elif guard == "interrupted":
            sessions._interrupt_fenced_sessions.add(parent.id)
        elif guard == "nested":
            recovery.rows[parent.id] = replace(parent, parent_conversation_id="grandparent")
        elif guard == "parent_rebound":
            recovery.rows[parent.id] = replace(parent, runner_id="replacement")
        elif guard == "child_rebound":
            recovery.rows[child.id] = replace(child, runner_id="replacement")
        elif guard == "reparented":
            recovery.rows[child.id] = replace(child, parent_conversation_id="another_parent")
        elif guard == "owner_mismatch":
            recovery.owners[parent.id] = "someone_else"
        elif guard == "child_owner_mismatch":
            recovery.owners[child.id] = "someone_else"
        elif guard == "owner_unknown":
            recovery.owners[parent.id] = None
        elif guard == "missing_parent":
            recovery.rows.pop(parent.id)
        elif guard == "missing_child":
            recovery.rows.pop(child.id)
        elif guard == "removed_agent":
            recovery.rows[parent.id] = replace(parent, agent_id=None)
        else:
            recovery.read_error = True

    try:
        if phase == "before_init":
            change()
        else:
            setattr(recovery, phase, change)
        assert await recover(recovery) is None
    finally:
        sessions._interrupt_fenced_sessions.discard(recovery.parent.id)
    assert [path for path, _ in recovery.requests] == (
        []
        if phase == "before_init"
        else ["/v1/sessions", "/v1/sessions/child/events"]
        if phase == "after_delivery"
        else ["/v1/sessions"]
    )


@pytest.mark.parametrize("phase", ["after_init", "after_relay", "after_delivery"])
async def test_parent_recovery_never_acknowledges_a_replacement_generation(
    recovery: Any,
    phase: str,
) -> None:
    def reconnect() -> None:
        recovery.generation += 1

    setattr(recovery, phase, reconnect)
    assert await recover(recovery) is None
    paths = [path for path, _ in recovery.requests]
    assert paths == (
        ["/v1/sessions", "/v1/sessions/child/events"]
        if phase == "after_delivery"
        else ["/v1/sessions"]
    )


@pytest.mark.parametrize("init_status", [201, 302, 410, 503])
@pytest.mark.parametrize("delivery_status", [204, 200, 202, 302, 503])
async def test_only_successful_init_and_confirmed_delivery_are_acknowledged(
    recovery: Any,
    init_status: int,
    delivery_status: int,
) -> None:
    recovery.init_status = init_status
    recovery.delivery_status = delivery_status
    result = await recover(recovery)
    if init_status == 201 and delivery_status == 204:
        assert result is not None and result.status_code == 204
    else:
        assert result is None
    assert len(recovery.requests) == (2 if init_status == 201 else 1)
    assert recovery.parent.live_status == "failed"
    assert recovery.parent.labels == {"omnigent.last_task_error_code": "provider_error"}
    payload = recovery.requests[0][1]["session_init"]
    assert payload["suppress_recovery_turn"] is True
    assert payload["resume_interrupted_turn"] is False


async def test_initialization_cannot_bypass_saved_inference_configuration(recovery: Any) -> None:
    recovery.rows["parent"] = replace(
        recovery.parent, inference_snapshot={"runtime_config": {"providers": {}}}
    )
    assert await recover(recovery) is None
    assert [path for path, _ in recovery.requests] == ["/v1/sessions"]


async def test_cold_parent_recovery_keeps_same_validated_client(
    recovery: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = AsyncMock(return_value=recovery.client)
    monkeypatch.setattr(sessions, "_get_runner_client", resolve)
    result = await recover(recovery)
    assert result is not None and result.status_code == 204
    resolve.assert_awaited_once()
    assert recovery.requests[-1] == ("/v1/sessions/child/events", recovery.body)
