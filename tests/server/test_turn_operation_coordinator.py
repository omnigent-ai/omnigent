"""Tests for crash-aware server-to-runner turn dispatch coordination."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from omnigent.server.turn_operation_coordinator import (
    RunnerOperationDispatchUnknown,
    RunnerOperationRejected,
    RunnerOperationUnavailable,
    TurnOperationCoordinator,
)
from omnigent.stores.turn_operation_store.sqlalchemy_store import (
    SqlAlchemyTurnOperationStore,
)


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@dataclass
class _Runner:
    incarnation: str = field(default_factory=lambda: _uid("runner-a"))
    operations: dict[str, dict[str, str]] = field(default_factory=dict)
    posts: list[dict[str, Any]] = field(default_factory=list)
    executions: list[str] = field(default_factory=list)
    operation_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    post_mode: str = "accept"

    def handler(self, request: httpx.Request) -> httpx.Response:
        operation_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            operation = self.operations.get(operation_id)
            if operation is None:
                return httpx.Response(
                    404,
                    json={
                        "error": "operation_not_found",
                        "runner_incarnation_id": self.incarnation,
                    },
                )
            return httpx.Response(
                200,
                json={**operation, "runner_incarnation_id": self.incarnation},
            )

        body = json.loads(request.content)
        self.posts.append(body)
        if self.post_mode == "reject":
            return httpx.Response(409, json={"error": "session_busy", "detail": "busy"})
        if self.post_mode == "malformed":
            return httpx.Response(202, json={"state": "running"})
        existing = self.operations.get(body["operation_id"])
        if existing is not None:
            if self.operation_requests[body["operation_id"]] != body:
                return httpx.Response(
                    409,
                    json={"error": "operation_conflict", "detail": "changed request"},
                )
            return httpx.Response(
                202, json={**existing, "runner_incarnation_id": self.incarnation}
            )
        operation = {
            "operation_id": body["operation_id"],
            "session_id": request.url.path.split("/")[3],
            "state": "running",
        }
        self.operations[body["operation_id"]] = operation
        self.operation_requests[body["operation_id"]] = body
        self.executions.append(body["operation_id"])
        if self.post_mode == "timeout":
            raise httpx.ReadTimeout("response lost after acceptance", request=request)
        return httpx.Response(202, json={**operation, "runner_incarnation_id": self.incarnation})


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyTurnOperationStore:
    return SqlAlchemyTurnOperationStore(db_uri)


def _input_persisted(
    store: SqlAlchemyTurnOperationStore,
    *,
    session_id: str | None = None,
):
    session_id = session_id or _uid("session")
    operation, _ = store.create_or_get(
        conversation_id=session_id,
        principal="agentfactory:workflow-principal",
        idempotency_key="attempt-1",
        request={"content": "build it", "version": "v1alpha1"},
    )
    return store.mark_input_persisted(
        operation.id,
        _uid("item"),
        {"type": "message", "content": "build it"},
    )


def _coordinator(
    store: SqlAlchemyTurnOperationStore,
    runner: _Runner,
) -> tuple[TurnOperationCoordinator, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(runner.handler),
        base_url="http://runner",
    )
    return TurnOperationCoordinator(store, client), client


async def test_dispatch_binds_incarnation_and_reconcile_terminal(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    runner = _Runner()
    coordinator, client = _coordinator(store, runner)
    async with client:
        dispatched = await coordinator.dispatch(
            operation.id,
            operation.conversation_id,
        )
        assert dispatched.state == "dispatched"
        assert dispatched.runner_incarnation_id == runner.incarnation
        assert dispatched.dispatch_attempts == 1
        assert runner.posts == [
            {
                "type": "message",
                "content": "build it",
                "operation_id": operation.id,
            }
        ]

        runner.operations[operation.id]["state"] = "succeeded"
        terminal = await coordinator.reconcile(operation.id, operation.conversation_id)
    assert terminal.state == "succeeded"
    assert terminal.terminal_at is not None


async def test_transport_unknown_retries_only_on_same_incarnation(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    runner = _Runner(post_mode="timeout")
    coordinator, client = _coordinator(store, runner)
    async with client:
        with pytest.raises(RunnerOperationDispatchUnknown) as caught:
            await coordinator.dispatch(operation.id, operation.conversation_id)
        assert caught.value.operation.state == "dispatch_unknown"
        assert caught.value.operation.runner_incarnation_id == runner.incarnation

        runner.post_mode = "accept"
        dispatched = await coordinator.dispatch(
            operation.id,
            operation.conversation_id,
        )
    assert dispatched.state == "dispatched"
    assert dispatched.dispatch_attempts == 2
    assert dispatched.error_code is None
    assert dispatched.error is None
    assert len(runner.posts) == 2
    assert runner.executions == [operation.id]


async def test_changed_incarnation_never_redispatches_unknown_operation(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    runner = _Runner(post_mode="timeout")
    coordinator, client = _coordinator(store, runner)
    async with client:
        with pytest.raises(RunnerOperationDispatchUnknown):
            await coordinator.dispatch(operation.id, operation.conversation_id)
        runner.incarnation = _uid("runner-b")
        runner.post_mode = "accept"
        terminal = await coordinator.dispatch(
            operation.id,
            operation.conversation_id,
        )
    assert terminal.state == "timed_out"
    assert terminal.error_code == "runner_restart_ambiguous"
    assert len(runner.posts) == 1


async def test_restart_after_recorded_attempt_crash_window_is_terminal_ambiguity(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    store.record_dispatch_attempt(operation.id, _uid("runner-a"))
    runner = _Runner(incarnation=_uid("runner-b"))
    coordinator, client = _coordinator(store, runner)
    async with client:
        terminal = await coordinator.dispatch(
            operation.id,
            operation.conversation_id,
        )
    assert terminal.state == "timed_out"
    assert terminal.error_code == "runner_restart_ambiguous"
    assert runner.posts == []


async def test_preflight_transport_failure_does_not_record_attempt(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable),
        base_url="http://runner",
    )
    coordinator = TurnOperationCoordinator(store, client)
    async with client:
        with pytest.raises(RunnerOperationUnavailable):
            await coordinator.dispatch(operation.id, operation.conversation_id)
    durable = store.get(operation.id)
    assert durable is not None
    assert durable.state == "input_persisted"
    assert durable.dispatch_attempts == 0
    assert durable.runner_incarnation_id is None


async def test_definitive_runner_rejection_is_retryable_without_unknown_claim(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    runner = _Runner(post_mode="reject")
    coordinator, client = _coordinator(store, runner)
    async with client:
        with pytest.raises(RunnerOperationRejected) as caught:
            await coordinator.dispatch(operation.id, operation.conversation_id)
    assert caught.value.status_code == 409
    durable = store.get(operation.id)
    assert durable is not None
    assert durable.state == "input_persisted"
    assert durable.dispatch_attempts == 1
    assert durable.runner_incarnation_id == runner.incarnation


async def test_malformed_acceptance_becomes_dispatch_unknown(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    runner = _Runner(post_mode="malformed")
    coordinator, client = _coordinator(store, runner)
    async with client:
        with pytest.raises(RunnerOperationDispatchUnknown) as caught:
            await coordinator.dispatch(operation.id, operation.conversation_id)
    assert caught.value.operation.state == "dispatch_unknown"
    assert caught.value.operation.error_code == "runner_protocol_unknown"


async def test_same_incarnation_missing_after_ack_terminalizes_without_post(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    runner = _Runner()
    store.record_dispatch_attempt(operation.id, runner.incarnation)
    dispatched = store.mark_dispatch_result(operation.id, state="dispatched")
    coordinator, client = _coordinator(store, runner)
    async with client:
        terminal = await coordinator.reconcile(dispatched.id, dispatched.conversation_id)
    assert terminal.state == "timed_out"
    assert terminal.error_code == "runner_operation_missing"
    assert runner.posts == []


async def test_terminal_operation_is_replayed_without_runner_access(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation = _input_persisted(store)
    incarnation = _uid("runner-a")
    store.record_dispatch_attempt(operation.id, incarnation)
    store.mark_dispatch_result(operation.id, state="dispatched")
    terminal = store.mark_terminal(operation.id, state="failed")

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("terminal replay must not access runner")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(forbidden),
        base_url="http://runner",
    )
    coordinator = TurnOperationCoordinator(store, client)
    async with client:
        replay = await coordinator.dispatch(terminal.id, terminal.conversation_id)
    assert replay == terminal
