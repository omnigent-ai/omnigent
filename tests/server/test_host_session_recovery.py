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
from omnigent.server.host_session_recovery import HostSessionRecoverer
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.session_recovery import (
    RECOVERY_MAX_AGE_S,
    RECOVERY_NOTICE,
    RECOVERY_PROMPT,
)
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
        registered_with_managed_token=True,
    )
    hosts = Mock(get=lambda _: connection)
    host = SimpleNamespace(
        host_id=host_id,
        sandbox_provider="test",
        deleted_at=None,
        user_id="owner",
    )
    host_store = Mock(get_host=Mock(return_value=host))
    host_config: dict[str, object] = {"host": {"recover_interrupted_sessions": True}}
    sandbox_config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.test",
            launcher_factory=Mock(),
            token_ttl_s=3600,
            provider="test",
            host_config=host_config,
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
        intent = store.get_session_recovery(conv.id)
        launching = replace(intent, launch_runner_id="new")
        store.replace_session_recovery(conv.id, intent, launching)
        store.replace_runner_id(conv.id, "new")
        store.replace_session_recovery(conv.id, launching, replace(launching, runner_id="new"))
        online.add("new")
        return SimpleNamespace(runner_id="new", error=None, error_code=None)

    launch_mock = AsyncMock(side_effect=launch)
    monkeypatch.setattr("omnigent.server.routes.sessions._launch_runner_on_host", launch_mock)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._query_host_runner_status", AsyncMock(return_value="dead")
    )
    publish_status = Mock()
    monkeypatch.setattr("omnigent.server.routes.sessions._publish_status", publish_status)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._persist_session_status_error_labels", AsyncMock()
    )
    clock = [1000]
    monkeypatch.setattr("omnigent.server.host_session_recovery.now_epoch", lambda: clock[0])
    service = HostSessionRecoverer(
        store, hosts, host_store, runners, initializer, sandbox_config=sandbox_config
    )
    return SimpleNamespace(
        service=service,
        store=store,
        conv=conv,
        connection=connection,
        host=host,
        host_config=host_config,
        sandbox_config=sandbox_config,
        initializer=initializer,
        publish_status=publish_status,
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
@pytest.mark.parametrize("custom_prompt", [None, "Check the task checkpoint before continuing."])
async def test_host_recovery_claim_is_shared_between_replicas(
    recovery: Any, custom_prompt: str | None
) -> None:
    if custom_prompt is not None:
        recovery.host_config["host"]["recovery_prompt"] = custom_prompt
    service = recovery.service
    other = HostSessionRecoverer(
        recovery.store,
        service.hosts,
        service.host_store,
        service.runners,
        service.initializer,
        sandbox_config=recovery.sandbox_config,
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
    assert recovery.initializer.initialize.call_args.kwargs["recovery_prompt"] == (
        f"{RECOVERY_NOTICE}\n{custom_prompt}" if custom_prompt is not None else RECOVERY_PROMPT
    )
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
        "other_replica",
        "other_owner",
    ],
)
async def test_ineligible_session_is_not_restarted(recovery: Any, reason: str) -> None:
    if reason == "idle":
        recovery.store.set_session_live_status(recovery.conv.id, "idle")
    elif reason == "approval":
        recovery.store.set_pending_elicitation_count(recovery.conv.id, 1)
    elif reason == "stopped":
        recovery.service.stop_recovery(recovery.conv)
    elif reason == "survived":
        recovery.online.add("old")
    elif reason == "other_replica":
        from omnigent.db.utils import now_epoch

        recovery.store.touch_runner_liveness(["old"], now_epoch())
    elif reason == "other_owner":
        recovery.store.get_session_owner = Mock(return_value="someone-else")
    await recovery.service.recover(recovery.connection, [])
    recovery.launch.assert_not_awaited()
    recovery.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "missing_config",
        "missing_host_config",
        "missing_host_section",
        "missing_opt_in",
        "host_opt_out",
        "ordinary_host",
        "string_true",
        "integer_one",
        "unknown_provider",
        "deleted_host",
        "missing_host",
        "unsupported_store",
    ],
)
async def test_admin_opt_in_gates_bookkeeping_and_continuation(recovery: Any, reason: str) -> None:
    service = recovery.service
    if reason == "missing_config":
        service = HostSessionRecoverer(
            recovery.store, service.hosts, service.host_store, service.runners, service.initializer
        )
    elif reason == "missing_host_config":
        recovery.host_config.clear()
    elif reason == "missing_host_section":
        recovery.host_config["host"] = None
    elif reason == "missing_opt_in":
        recovery.host_config["host"].clear()
    elif reason == "host_opt_out":
        recovery.connection.hello.recover_interrupted_sessions = False
    elif reason == "ordinary_host":
        recovery.connection.registered_with_managed_token = False
    elif reason in {"disabled", "string_true", "integer_one"}:
        recovery.host_config["host"]["recover_interrupted_sessions"] = {
            "disabled": False,
            "string_true": "true",
            "integer_one": 1,
        }[reason]
    elif reason == "unknown_provider":
        recovery.host.sandbox_provider = "unknown"
    elif reason == "deleted_host":
        recovery.host.deleted_at = 1000
    elif reason == "missing_host":
        service.host_store.get_host.return_value = None
    else:
        recovery.store.supports_session_recovery = False
    if reason != "host_opt_out":
        del recovery.connection.hello
    recovery.store.replace_session_recovery = Mock(wraps=recovery.store.replace_session_recovery)
    assert not service.enabled(recovery.conv.host_id)
    service.record_interruption(recovery.conv)
    service.stop_recovery(recovery.conv)
    await service.recover(recovery.connection, [])
    recovery.store.replace_session_recovery.assert_not_called()
    assert recovery.store.get_session_recovery(recovery.conv.id) is None
    recovery.launch.assert_not_awaited()
    recovery.initializer.initialize.assert_not_awaited()


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param("", id="empty"),
        pytest.param(" \n ", id="blank"),
        pytest.param(123, id="number"),
        pytest.param(True, id="boolean"),
        pytest.param("x" * 8001, id="too-long"),
    ],
)
def test_invalid_custom_recovery_prompt_is_rejected(recovery: Any, prompt: object) -> None:
    recovery.host_config["host"]["recovery_prompt"] = prompt
    service = recovery.service
    with pytest.raises(
        ValueError, match=r"host\.recovery_prompt must contain 1 to 8000 characters"
    ):
        HostSessionRecoverer(
            service.store,
            service.hosts,
            service.host_store,
            service.runners,
            service.initializer,
            sandbox_config=recovery.sandbox_config,
        )


def test_custom_recovery_prompt_accepts_the_maximum_length(recovery: Any) -> None:
    prompt = "x" * 8000
    recovery.host_config["host"]["recovery_prompt"] = prompt
    service = recovery.service
    configured = HostSessionRecoverer(
        service.store,
        service.hosts,
        service.host_store,
        service.runners,
        service.initializer,
        sandbox_config=recovery.sandbox_config,
    )
    assert configured.prompt(recovery.conv.host_id) == f"{RECOVERY_NOTICE}\n{prompt}"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pending", "stopped"])
async def test_disabling_recovery_leaves_existing_intent_untouched(
    recovery: Any, phase: str
) -> None:
    service = recovery.service
    if phase == "stopped":
        service.stop_recovery(recovery.conv)
    else:
        service.record_interruption(recovery.conv)
    previous = recovery.store.get_session_recovery(recovery.conv.id)
    assert previous.phase == phase
    recovery.host_config["host"]["recover_interrupted_sessions"] = False
    recovery.store.replace_session_recovery = Mock(wraps=recovery.store.replace_session_recovery)
    service.record_interruption(recovery.conv)
    service.stop_recovery(recovery.conv)
    service.stop_recovery(recovery.conv, only_pending=True)
    await service.recover(recovery.connection, [])
    recovery.store.replace_session_recovery.assert_not_called()
    assert recovery.store.get_session_recovery(recovery.conv.id) == previous
    recovery.launch.assert_not_awaited()
    recovery.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["message", "stop", "opt_out", "rebind"])
async def test_user_intent_during_launch_prevents_continuation(recovery: Any, action: str) -> None:
    launch = recovery.launch.side_effect

    async def interrupted_launch(*args: Any, **kwargs: Any) -> Any:
        result = await launch(*args, **kwargs)
        if action == "message":
            user_input(recovery.store, recovery.conv.id)
        elif action == "stop":
            recovery.service.stop_recovery(recovery.conv)
        elif action == "rebind":
            recovery.store.replace_runner_id(recovery.conv.id, "explicit")
        else:
            recovery.host_config["host"]["recover_interrupted_sessions"] = False
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
            recovery.store,
            service.hosts,
            service.host_store,
            service.runners,
            service.initializer,
            sandbox_config=recovery.sandbox_config,
        )
        await service.recover(recovery.connection, [])
        state = recovery.store.get_session_recovery(recovery.conv.id)
        assert state.attempts == expected
        recovery.clock[0] += 300
    assert state.phase == "failed"
    assert recovery.publish_status.call_args.args[2].message == (
        "Automatic resume failed. Please send a new message to continue."
    )
    assert recovery.publish_status.call_args.kwargs["failure_origin"] == "host_session_recovery"
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


def test_stop_survives_store_restart_and_fresh_user_input_reenables_recovery(
    recovery: Any, db_uri: str
) -> None:
    recovery.service.stop_recovery(recovery.conv)
    restarted = SqlAlchemyConversationStore(db_uri)
    service = recovery.service
    restarted_service = HostSessionRecoverer(
        restarted,
        service.hosts,
        service.host_store,
        service.runners,
        service.initializer,
        sandbox_config=recovery.sandbox_config,
    )
    restarted_service.record_interruption(recovery.conv)
    assert restarted.get_session_recovery(recovery.conv.id).phase == "stopped"
    user_input(restarted, recovery.conv.id)
    restarted_service.record_interruption(recovery.conv)
    assert restarted.get_session_recovery(recovery.conv.id).phase == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_binding_committed", [False, True])
@pytest.mark.parametrize("runner_online", [False, True])
@pytest.mark.parametrize("explicit_rebind", [False, True])
async def test_server_restart_finishes_launch_after_runner_binding_rotation(
    recovery: Any, claim_binding_committed: bool, runner_online: bool, explicit_rebind: bool
) -> None:
    recovery.service.record_interruption(recovery.conv)
    intent = recovery.store.get_session_recovery(recovery.conv.id)
    launching = replace(
        intent, phase="launching", attempts=1, attempt_id="durable", launch_runner_id="new"
    )
    assert recovery.store.replace_session_recovery(recovery.conv.id, intent, launching)
    recovery.store.replace_runner_id(recovery.conv.id, "new")
    if claim_binding_committed:
        assert recovery.store.replace_session_recovery(
            recovery.conv.id, launching, replace(launching, runner_id="new")
        )
    if runner_online:
        recovery.online.add("new")
    if explicit_rebind:
        recovery.store.replace_runner_id(recovery.conv.id, "explicit")
        recovery.online.add("explicit")
    await recovery.service.recover(recovery.connection, ["new"] if runner_online else [])
    if explicit_rebind:
        recovery.launch.assert_not_awaited()
        recovery.initializer.initialize.assert_not_awaited()
        assert recovery.store.get_conversation(recovery.conv.id).runner_id == "explicit"
        return
    assert recovery.launch.await_count == (0 if runner_online else 1)
    recovery.initializer.initialize.assert_awaited_once()
    state = recovery.store.get_session_recovery(recovery.conv.id)
    assert state.phase == "submitted"
    assert state.runner_id == "new"
    assert state.attempts == (1 if runner_online else 2)
    if runner_online:
        assert recovery.initializer.initialize.call_args.kwargs["recovery_id"] == "durable"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["runner_disconnected", "runner_interrupted"])
@pytest.mark.parametrize("age", [60, RECOVERY_MAX_AGE_S + 1])
async def test_failed_session_keeps_its_original_interruption_time(
    recovery: Any, error_code: str, age: int
) -> None:
    interrupted_at = recovery.clock[0] - age
    recovery.store.set_labels(
        recovery.conv.id, {"omnigent.last_task_error_code": error_code}, updated_at=interrupted_at
    )
    recovery.store.set_session_live_status(recovery.conv.id, "failed")
    await recovery.service.recover(recovery.connection, [])
    state = recovery.store.get_session_recovery(recovery.conv.id)
    assert state.interrupted_at == interrupted_at
    if age > RECOVERY_MAX_AGE_S:
        assert state.phase == "failed"
        recovery.launch.assert_not_awaited()
        recovery.initializer.initialize.assert_not_awaited()
    else:
        recovery.initializer.initialize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_online", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_continuation_deadline_also_applies_to_a_successful_final_launch(
    recovery: Any, already_online: bool, expired: bool
) -> None:
    recovery.service.record_interruption(recovery.conv)
    intent = recovery.store.get_session_recovery(recovery.conv.id)
    launching = replace(
        intent,
        phase="launching" if already_online else "pending",
        attempts=3 if already_online else 2,
        attempt_id="final-attempt",
        launch_runner_id="new" if already_online else None,
        runner_id="new" if already_online else "old",
    )
    assert recovery.store.replace_session_recovery(recovery.conv.id, intent, launching)
    recovery.clock[0] += RECOVERY_MAX_AGE_S
    if already_online:
        recovery.store.replace_runner_id(recovery.conv.id, "new")
        recovery.online.add("new")
        recovery.clock[0] += int(expired)
    else:
        launch = recovery.launch.side_effect

        async def delayed_launch(*args: Any, **kwargs: Any) -> Any:
            result = await launch(*args, **kwargs)
            recovery.clock[0] += int(expired)
            return result

        recovery.launch.side_effect = delayed_launch
    await recovery.service.recover(recovery.connection, [])
    state = recovery.store.get_session_recovery(recovery.conv.id)
    assert state.attempts == 3
    assert state.phase == ("failed" if expired else "submitted")
    assert recovery.initializer.initialize.await_count == (0 if expired else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["current", "other_host", "other_runner"])
async def test_server_restart_bounds_unconfirmed_dispatch_to_an_online_runner(
    recovery: Any, binding: str
) -> None:
    recovery.service.record_interruption(recovery.conv)
    intent = recovery.store.get_session_recovery(recovery.conv.id)
    dispatching = replace(
        intent,
        phase="dispatching",
        host_id="b" * 32 if binding == "other_host" else intent.host_id,
        runner_id="new" if binding == "other_runner" else intent.runner_id,
        attempts=1,
        attempt_id="unconfirmed",
        retry_at=recovery.clock[0] + 1,
    )
    assert recovery.store.replace_session_recovery(recovery.conv.id, intent, dispatching)
    recovery.online.add("old")
    await recovery.service.recover(recovery.connection, ["old"])
    assert recovery.store.get_session_recovery(recovery.conv.id) == dispatching
    recovery.clock[0] += 1
    await recovery.service.recover(recovery.connection, ["old"])
    expected = replace(dispatching, phase="failed") if binding == "current" else dispatching
    assert recovery.store.get_session_recovery(recovery.conv.id) == expected
    assert recovery.publish_status.call_count == (1 if binding == "current" else 0)
    recovery.launch.assert_not_awaited()
    recovery.initializer.initialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_then_unarchive_does_not_resume_old_work(recovery: Any) -> None:
    recovery.service.record_interruption(recovery.conv)
    recovery.store.update_conversation(recovery.conv.id, archived=True)
    recovery.store.update_conversation(recovery.conv.id, archived=False)
    await recovery.service.recover(recovery.connection, [])
    recovery.launch.assert_not_awaited()
