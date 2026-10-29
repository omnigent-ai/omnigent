"""Host-requested continuation uses durable claims and yields to user intent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.host_session_recovery import (
    HostSessionRecoverer,
    record_interruption,
    stop_recovery,
)
from omnigent.session_recovery import RECOVERY_PROMPT, SessionRecovery
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def recovery(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    store = SqlAlchemyConversationStore(db_uri)
    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "recovery", "bundle")
    host_id = "a" * 32
    conv = store.create_conversation(
        agent_id=agent.id, host_id=host_id, workspace="/workspace", runner_id="old"
    )
    store.set_session_live_status(conv.id, "running")
    conv = store.get_conversation(conv.id)
    connection = SimpleNamespace(
        host_id=host_id,
        owner="owner",
        hello=SimpleNamespace(recover_interrupted_sessions=True),
    )
    hosts = Mock(get=lambda _: connection)
    host_store = Mock(
        get_host=lambda _: SimpleNamespace(
            sandbox_provider="test",
            deleted_at=None,
            user_id="owner",
        )
    )
    online: set[str] = set()
    runners = Mock(
        runner_is_online=lambda runner_id: runner_id in online,
        client_for_existing_conversation=lambda _: SimpleNamespace(runner_id="new", client=Mock()),
    )
    initializer = Mock(
        initialize=AsyncMock(
            return_value=httpx.Response(
                201,
                json={"recovery_turn": "recovery_prompt", "status": "running"},
                request=httpx.Request("POST", "http://runner/v1/sessions"),
            )
        )
    )

    async def launch(*_args: Any, **_kwargs: Any) -> Any:
        store.replace_runner_id(conv.id, "new")
        intent = store.get_session_recovery(conv.id)
        store.replace_session_recovery(conv.id, intent, replace(intent, runner_id="new"))
        online.add("new")
        return SimpleNamespace(runner_id="new", error=None, error_code=None)

    launch_mock = AsyncMock(side_effect=launch)
    monkeypatch.setattr("omnigent.server.routes.sessions._launch_runner_on_host", launch_mock)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._query_host_runner_status", AsyncMock(return_value="dead")
    )
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_status", Mock())
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._persist_session_status_error_labels", AsyncMock()
    )
    clock = [1000]
    monkeypatch.setattr("omnigent.server.host_session_recovery.now_epoch", lambda: clock[0])
    service = HostSessionRecoverer(store, hosts, host_store, runners, initializer)
    return SimpleNamespace(
        service=service,
        store=store,
        conv=conv,
        connection=connection,
        initializer=initializer,
        launch=launch_mock,
        online=online,
        clock=clock,
    )


def user_input(
    store: SqlAlchemyConversationStore, session_id: str, text: str = "New work"
) -> None:
    store.append(
        session_id,
        [
            NewConversationItem(
                response_id="user",
                type="message",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
        ],
    )


@pytest.mark.asyncio
async def test_host_recovery_claim_is_shared_between_replicas(recovery: Any) -> None:
    service = recovery.service
    other = HostSessionRecoverer(
        recovery.store, service.hosts, service.host_store, service.runners, service.initializer
    )
    await asyncio.gather(
        service.recover(recovery.connection, []), other.recover(recovery.connection, [])
    )
    recovery.launch.assert_awaited_once()
    recovery.initializer.initialize.assert_awaited_once()
    state = recovery.store.get_session_recovery(recovery.conv.id)
    assert state.phase == "submitted"
    assert state.attempts == 1
    assert recovery.initializer.initialize.call_args.kwargs["recovery_id"] == state.attempt_id
    await service.recover(recovery.connection, ["new"])
    recovery.initializer.initialize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "idle",
        "approval",
        "stopped",
        "survived",
        "opt_out",
        "other_replica",
        "other_owner",
        "unsupported_store",
    ],
)
async def test_ineligible_session_is_not_restarted(recovery: Any, reason: str) -> None:
    if reason == "idle":
        recovery.store.set_session_live_status(recovery.conv.id, "idle")
    elif reason == "approval":
        recovery.store.set_pending_elicitation_count(recovery.conv.id, 1)
    elif reason == "stopped":
        stop_recovery(recovery.store, recovery.conv)
    elif reason == "survived":
        recovery.online.add("old")
    elif reason == "other_replica":
        from omnigent.db.utils import now_epoch

        recovery.store.touch_runner_liveness(["old"], now_epoch())
    elif reason == "other_owner":
        recovery.store.get_session_owner = Mock(return_value="someone-else")
    elif reason == "unsupported_store":
        recovery.store.supports_session_recovery = False
    else:
        recovery.connection.hello.recover_interrupted_sessions = False
    await recovery.service.recover(recovery.connection, [])
    recovery.launch.assert_not_awaited()
    recovery.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["message", "stop"])
async def test_user_intent_during_launch_prevents_continuation(recovery: Any, action: str) -> None:
    launch = recovery.launch.side_effect

    async def interrupted_launch(*args: Any, **kwargs: Any) -> Any:
        result = await launch(*args, **kwargs)
        if action == "message":
            user_input(recovery.store, recovery.conv.id)
        else:
            stop_recovery(recovery.store, recovery.conv)
        return result

    recovery.launch.side_effect = interrupted_launch
    await recovery.service.recover(recovery.connection, [])
    recovery.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_retries_stop_after_three_failures_across_service_restarts(recovery: Any) -> None:
    recovery.launch.side_effect = RuntimeError("runner spawn failed")
    service = recovery.service
    for expected in range(1, 4):
        service = HostSessionRecoverer(
            recovery.store, service.hosts, service.host_store, service.runners, service.initializer
        )
        await service.recover(recovery.connection, [])
        state = recovery.store.get_session_recovery(recovery.conv.id)
        assert state.attempts == expected
        recovery.clock[0] += 300
    assert state.phase == "failed"
    await service.recover(recovery.connection, [])
    assert recovery.launch.await_count == 3
    recovery.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_submission_outcome_is_never_blindly_replayed(recovery: Any) -> None:
    recovery.initializer.initialize.side_effect = httpx.ReadTimeout("ack lost")
    await recovery.service.recover(recovery.connection, [])
    state = recovery.store.get_session_recovery(recovery.conv.id)
    assert state.phase == "failed"
    recovery.online.clear()
    recovery.clock[0] += 300
    await recovery.service.recover(recovery.connection, [])
    recovery.initializer.initialize.assert_awaited_once()


def test_input_cancels_claim_atomically_and_native_notice_keeps_it(recovery: Any) -> None:
    store, conv = recovery.store, recovery.conv
    record_interruption(store, conv)
    state = store.get_session_recovery(conv.id)
    assert isinstance(state, SessionRecovery)
    user_input(store, conv.id, RECOVERY_PROMPT)
    assert store.get_session_recovery(conv.id) == state
    items = store.list_items(conv.id).data
    assert items[-1].data.is_meta is True
    user_input(store, conv.id)
    assert not store.replace_session_recovery(conv.id, state, replace(state, phase="dispatching"))
    assert store.get_session_recovery(conv.id) is None


def test_stop_survives_store_restart_and_fresh_user_input_reenables_recovery(
    recovery: Any, db_uri: str
) -> None:
    stop_recovery(recovery.store, recovery.conv)
    restarted = SqlAlchemyConversationStore(db_uri)
    record_interruption(restarted, recovery.conv)
    assert restarted.get_session_recovery(recovery.conv.id).phase == "stopped"
    user_input(restarted, recovery.conv.id)
    record_interruption(restarted, recovery.conv)
    assert restarted.get_session_recovery(recovery.conv.id).phase == "pending"


@pytest.mark.asyncio
async def test_server_restart_finishes_claim_for_already_launched_runner(recovery: Any) -> None:
    record_interruption(recovery.store, recovery.conv)
    intent = recovery.store.get_session_recovery(recovery.conv.id)
    launching = replace(intent, phase="launching", attempts=1, attempt_id="durable")
    recovery.store.replace_session_recovery(recovery.conv.id, intent, launching)
    await recovery.launch()
    recovery.launch.reset_mock()
    await recovery.service.recover(recovery.connection, ["new"])
    recovery.launch.assert_not_awaited()
    recovery.initializer.initialize.assert_awaited_once()
    assert recovery.initializer.initialize.call_args.kwargs["recovery_id"] == "durable"


@pytest.mark.asyncio
async def test_archive_then_unarchive_does_not_resume_old_work(recovery: Any) -> None:
    record_interruption(recovery.store, recovery.conv)
    recovery.store.update_conversation(recovery.conv.id, archived=True)
    recovery.store.update_conversation(recovery.conv.id, archived=False)
    await recovery.service.recover(recovery.connection, [])
    recovery.launch.assert_not_awaited()
