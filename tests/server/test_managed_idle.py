"""Exercise durable idle/wake ordering with the existing host and provider seams."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostQuiesceFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.idle import CAP_IDLE_QUIESCE
from omnigent.runner.identity import token_bound_runner_id
from omnigent.server.host_registry import HostRegistry
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    resume_managed_host,
)
from omnigent.server.managed_idle import ManagedIdleSuspend
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import FakeSandboxLauncher


class AutoLauncher(FakeSandboxLauncher):
    provider = "modal"

    @property
    def capabilities(self):
        return replace(super().capabilities, managed_idle_suspend=True)

    def wait_for_idle(self, sandbox_id: str, generation: str) -> None:
        self.quiescent = (sandbox_id, generation)

    def stop(self, sandbox_id: str) -> None:
        self.stopped = sandbox_id


@pytest.fixture
def stack(db_uri: str):
    store = HostStore(db_uri)
    host = store.register_managed_host(
        host_id="7728298ff2a340f8a2b281d01fa36dcf",
        name="host",
        user_id="owner",
        token="initial-token",
        provider="modal",
        sandbox_id="actor",
        token_expires_at=now_epoch() + 3600,
    )
    store.upsert_on_connect(
        host_id=host.host_id, name="host", user_id="owner", managed_token="initial-token"
    )
    registry = HostRegistry()
    hello = HostHelloFrame(
        version="test", frame_protocol_version=1, name="host", capabilities=[CAP_IDLE_QUIESCE]
    )

    def connect(invocation=None):
        if invocation:
            store.upsert_on_connect(
                host_id=host.host_id, name="host", user_id="owner", managed_token=invocation.token
            )
        return registry.register(
            host.host_id, AsyncMock(), hello, "owner", registered_with_managed_token=True
        )

    connection = connect()
    launcher = AutoLauncher(can_resume=True, on_host_start=connect)
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.com",
            launcher_factory=lambda: launcher,
            token_ttl_s=3600,
            provider="modal",
        )
    )
    conversations = SqlAlchemyConversationStore(db_uri)
    coordinator = ManagedIdleSuspend(store, conversations, config, registry)
    return SimpleNamespace(
        store=store,
        host=host,
        registry=registry,
        connection=connection,
        launcher=launcher,
        config=config,
        conversations=conversations,
        coordinator=coordinator,
        connect=connect,
    )


def quiesce_reply(stack, monkeypatch, status="ok"):
    def send(connection, data):
        frame = decode_host_frame(data)
        assert isinstance(frame, HostQuiesceFrame)
        connection.pending_quiesce[frame.request_id].set_result({"status": status})
        if status == "ok":
            stack.registry.deregister(connection.host_id, conn=connection)

    monkeypatch.setattr(stack.registry, "send_text_direct", send)


async def test_suspended_host_wakes_with_same_identity(stack, monkeypatch):
    quiesce_reply(stack, monkeypatch)
    assert await stack.coordinator.suspend_if_idle(stack.connection)
    assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "suspended"
    assert stack.launcher.stopped == "actor"
    await resume_managed_host(stack.host.host_id, stack.store, stack.config)
    assert stack.store.is_online(stack.host.host_id)
    assert stack.store.get_host(stack.host.host_id).sandbox_id == "actor"
    assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "active"
    assert len(stack.launcher.host_starts) == 1


async def test_busy_host_is_not_suspended(stack, monkeypatch):
    quiesce_reply(stack, monkeypatch, "busy")
    assert not await stack.coordinator.suspend_if_idle(stack.connection)
    assert not hasattr(stack.launcher, "stopped")
    assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "active"


async def test_wake_waits_for_inflight_suspend(stack, monkeypatch):
    quiesce_reply(stack, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def stop(sandbox_id):
        entered.set()
        assert release.wait(5)
        stack.launcher.stopped = sandbox_id

    monkeypatch.setattr(stack.launcher, "stop", stop)
    suspend = asyncio.create_task(stack.coordinator.suspend_if_idle(stack.connection))
    wake = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        wake = asyncio.create_task(
            resume_managed_host(stack.host.host_id, stack.store, stack.config)
        )
        await asyncio.sleep(0)
        assert stack.launcher.resumed == []
        assert stack.store.claim_sandbox_operation(stack.host.host_id, "other", "wake") is None
    finally:
        release.set()
    assert await suspend
    assert wake is not None
    await wake
    assert stack.launcher.resumed == ["actor"]


async def test_cancelled_suspend_keeps_gate_closed_while_thread_finishes(stack, monkeypatch):
    quiesce_reply(stack, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def stop(_sandbox_id):
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    monkeypatch.setattr(stack.launcher, "stop", stop)
    suspend = asyncio.create_task(stack.coordinator.suspend_if_idle(stack.connection))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        suspend.cancel()
        with pytest.raises(asyncio.CancelledError):
            await suspend
        assert not finished.is_set()
        assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "unknown"
        with pytest.raises(HTTPException, match="unresolved"):
            await resume_managed_host(stack.host.host_id, stack.store, stack.config)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert stack.launcher.resumed == []


async def test_ambiguous_suspend_blocks_new_wake(stack, monkeypatch):
    quiesce_reply(stack, monkeypatch)

    def stop(_sandbox_id):
        raise TimeoutError("response lost")

    monkeypatch.setattr(stack.launcher, "stop", stop)
    with pytest.raises(TimeoutError):
        await stack.coordinator.suspend_if_idle(stack.connection)
    assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "unknown"
    with pytest.raises(HTTPException, match="unresolved"):
        await resume_managed_host(stack.host.host_id, stack.store, stack.config)
    assert stack.launcher.resumed == []


def launch_frame(stack):
    token = "new-runner-binding"
    conversation = stack.conversations.create_conversation(
        host_id=stack.host.host_id, workspace="/work"
    )
    stack.conversations.replace_runner_id(conversation.id, token_bound_runner_id(token))
    return HostLaunchRunnerFrame(
        request_id="request", binding_token=token, workspace="/work", session_id=conversation.id
    )


async def test_caller_timeout_does_not_release_launch_gate(stack):
    frame = launch_frame(stack)
    original = asyncio.get_running_loop().create_future()
    stack.connection.pending_launches[frame.request_id] = original
    stack.registry.idle_launch_dispatcher = stack.coordinator.dispatch
    stack.registry.send_text(stack.connection, encode_host_frame(frame))
    await poll_until_async(lambda: frame.request_id in stack.connection.pending_guarded_launches)
    original.cancel()
    assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "busy"
    stack.connection.pending_guarded_launches[frame.request_id].set_result({"status": "ok"})
    await asyncio.gather(*stack.coordinator.tasks)
    assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "active"


async def test_connection_replaced_before_send_returns_failed_result_not_exception(
    stack, monkeypatch
):
    frame = launch_frame(stack)

    async def replace_connection(*_args):
        stack.connect()

    monkeypatch.setattr(stack.registry, "admit_launch", replace_connection)
    original = asyncio.get_running_loop().create_future()
    await stack.coordinator.send_launch(stack.connection, frame, original)
    assert original.result()["status"] == "failed"
    assert stack.connection.outbound_queue.qsize() == 1  # retirement sentinel only
    assert stack.store.sandbox_operation(stack.host.host_id)["phase"] == "active"


def test_gate_does_not_expire_and_is_workspace_scoped(stack, monkeypatch):
    record = stack.store.claim_sandbox_operation(stack.host.host_id, "first", "suspend")
    assert record is not None
    monkeypatch.setattr("omnigent.stores.host_store.now_epoch", lambda: 9999999999)
    assert stack.store.claim_sandbox_operation(stack.host.host_id, "second", "wake") is None
    assert not stack.store.finish_sandbox_operation(stack.host.host_id, "second", "active")
    with workspace_scope(17):
        assert stack.store.sandbox_operation(stack.host.host_id) is None
    assert stack.store.finish_sandbox_operation(stack.host.host_id, "first", "suspended")


def test_detached_host_can_claim_replacement_start(stack):
    current = stack.store.get_host(stack.host.host_id)
    assert stack.store.detach_stale_managed_sandbox(
        current.host_id, sandbox_id="actor", expected_updated_at=current.updated_at
    )
    with pytest.raises(ValueError):
        stack.store.claim_sandbox_operation(current.host_id, "wake", "wake")
    assert stack.store.claim_sandbox_operation(current.host_id, "replace", "starting")


def test_deleted_host_cannot_be_resurrected_by_completion(stack):
    assert stack.store.claim_sandbox_operation(stack.host.host_id, "operation", "suspend")
    stack.store.delete_host(stack.host.host_id)
    stack.store.finish_sandbox_operation(stack.host.host_id, "operation", "suspended")
    assert stack.store.get_host(stack.host.host_id) is None
    with pytest.raises(ValueError):
        stack.store.claim_sandbox_operation(stack.host.host_id, "next", "wake")
