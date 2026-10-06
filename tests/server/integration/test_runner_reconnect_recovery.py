"""Reconnect recovery stays live when initialization or descendant work is slow."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.server.child_session_recovery import RECOVERY_STORE_CONCURRENCY
from omnigent.server.routes import runner_tunnel, sessions
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from tests.budgets import budget
from tests.server.integration.test_runner_tunnel_route import (
    _RUNNER_ID,
    _TUNNEL_PATH,
    _connect_route,
    _send_hello,
)

pytestmark = pytest.mark.asyncio


class _AsyncioProxy:
    def __init__(self, to_thread: Callable[..., Awaitable[Any]]) -> None:
        self.to_thread = to_thread

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


def _patch_to_thread(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    to_thread: Callable[..., Awaitable[Any]],
) -> None:
    """Intercept one module's offloads without changing global asyncio behavior."""
    monkeypatch.setattr(module, "asyncio", _AsyncioProxy(to_thread))


def _create_session(app: FastAPI, agent_id: str, **kwargs: Any) -> Conversation:
    store = app.state.runner_router._conversation_store
    conv = store.create_conversation(agent_id=agent_id, runner_id=_RUNNER_ID, **kwargs)
    store.set_session_live_status(conv.id, "failed")
    store.set_labels(
        conv.id,
        {
            "omnigent.last_task_error_code": "runner_disconnected",
            "omnigent.last_task_error_message": "Runner disconnected",
        },
    )
    sessions._session_status_cache[conv.id] = "failed"
    return conv


@asynccontextmanager
async def _recover(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> AsyncIterator[tuple[list[str], asyncio.Event]]:
    relays: list[str] = []
    finished = asyncio.Event()
    start = asyncio.Event()
    real_hook = runner_tunnel._run_connect_hook

    async def record_completion(hook: Any, connection: Any) -> None:
        await start.wait()
        await real_hook(hook, connection)
        finished.set()

    monkeypatch.setattr(runner_tunnel, "_run_connect_hook", record_completion)
    monkeypatch.setattr(sessions, "_ensure_runner_relay", lambda sid, *_, **__: relays.append(sid))
    monkeypatch.setattr(sessions, "RUNNER_DISCONNECT_GRACE_S", 0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runner"
    ) as client:
        app.state.runner_router._clients[_RUNNER_ID] = client
        communicator = await _connect_route(app, _TUNNEL_PATH)
        try:
            await _send_hello(communicator, app.state.tunnel_registry)
            asyncio.get_running_loop().call_soon(start.set)
            yield relays, finished
        finally:
            await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
            await communicator.wait(timeout=budget(5))
            grace = [
                task
                for task in asyncio.all_tasks()
                if task.get_name() == f"runner-disconnect-grace-{_RUNNER_ID}"
            ]
            await asyncio.gather(*grace)
            for (
                conv
            ) in app.state.runner_router._conversation_store.list_conversations_by_runner_id(
                _RUNNER_ID
            ):
                sessions._session_status_cache.pop(conv.id, None)


async def test_multiple_slow_roots_recover_independently_with_descendants(
    app: FastAPI, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    hung, slow, healthy = [_create_session(app, agent.id) for _ in range(3)]
    blocked_child = _create_session(
        app, agent.id, kind="sub_agent", parent_conversation_id=hung.id
    )
    slow_child = _create_session(app, agent.id, kind="sub_agent", parent_conversation_id=slow.id)
    healthy_child = _create_session(
        app, agent.id, kind="sub_agent", parent_conversation_id=healthy.id
    )
    all_ids = {c.id for c in (hung, slow, healthy, blocked_child, slow_child, healthy_child)}
    entered = {c.id: asyncio.Event() for c in (hung, slow, slow_child, healthy_child)}
    release = asyncio.Event()
    hung_stopped = asyncio.Event()
    requests: dict[str, tuple[dict[str, Any], str | None, set[str]]] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sid = body["session_id"]
        requests[sid] = (body, sessions._session_status_cache.get(sid), set(relays))
        if sid in entered:
            entered[sid].set()
        if sid in (hung.id, slow.id):
            try:
                await (asyncio.Event() if sid == hung.id else release).wait()
            finally:
                if sid == hung.id:
                    hung_stopped.set()
        return httpx.Response(201, json={})

    async with _recover(app, monkeypatch, respond) as (relays, finished):
        await asyncio.wait_for(
            asyncio.gather(*(entered[c.id].wait() for c in (hung, slow, healthy_child))), budget(5)
        )
        assert sessions._session_status_cache[hung.id] == "failed"
        assert sessions._session_status_cache[slow.id] == "failed"
        assert sessions._session_status_cache[healthy.id] == "idle"
        release.set()
        await asyncio.wait_for(entered[slow_child.id].wait(), budget(5))
        assert sessions._session_status_cache[slow.id] == "idle"
        assert sessions._session_status_cache[hung.id] == "failed"
        assert set(requests) == all_ids - {blocked_child.id}
        assert all(all_ids <= attached for _, _, attached in requests.values())
        for child in (slow_child, healthy_child):
            body, status, _ = requests[child.id]
            assert body["session_init"]["resume_interrupted_turn"] is True
            assert status == "failed"
        assert not finished.is_set()
    assert hung_stopped.is_set()
    assert not app.state.runner_session_initializer.invalidate_runner(_RUNNER_ID)


@pytest.mark.parametrize("superseded", [False, True])
async def test_large_mirror_tree_attaches_without_blocking_or_per_child_reads(
    app: FastAPI, db_uri: str, monkeypatch: pytest.MonkeyPatch, superseded: bool
) -> None:
    import omnigent.server.app as server_app

    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    parent = _create_session(app, agent.id)
    store = app.state.runner_router._conversation_store
    mirrors = []
    batch_size = server_app._RECONNECT_BINDING_BATCH_SIZE
    for _ in range(2 * batch_size + 1):
        child = _create_session(app, agent.id, kind="sub_agent", parent_conversation_id=parent.id)
        store.set_labels(child.id, {"omnigent.wrapper": "claude-code-native-ui-subagent"})
        mirrors.append(child.id)
    loop_thread = threading.get_ident()
    real_get = store.get_conversation
    real_get_many = store.get_conversations
    reads: list[tuple[str, int]] = []
    batches: list[set[str]] = []
    attachment_batches: list[set[str]] = []
    registry = app.state.tunnel_registry
    registry_get = registry.get

    def get_conversation(sid: str, *args: Any, **kwargs: Any) -> Any:
        reads.append((sid, threading.get_ident()))
        return real_get(sid, *args, **kwargs)

    def get_conversations(sids: list[str]) -> dict[str, Conversation]:
        batches.append(set(sids))
        return real_get_many(sids)

    monkeypatch.setattr(store, "get_conversation", get_conversation)
    monkeypatch.setattr(store, "get_conversations", get_conversations)
    heartbeats: list[int] = []
    stop = asyncio.Event()
    requests: list[str] = []

    async def read_then_replace(call: Any, *args: Any, **kwargs: Any) -> Any:
        result = await asyncio.to_thread(call, *args, **kwargs)
        if call == store.get_conversations and parent.id not in args[0] and not requests:
            attachment_batches.append(set(args[0]))
            if superseded and len(attachment_batches) == 2:
                current = registry_get(_RUNNER_ID)
                if current is not None:
                    newer = replace(current, generation=current.generation + 1)
                    # Exercise the generation check before route cancellation can stop recovery.
                    monkeypatch.setattr(registry, "get", lambda _: newer)
        return result

    _patch_to_thread(monkeypatch, server_app, read_then_replace)

    async def heartbeat() -> None:
        while not stop.is_set():
            heartbeats.append(len(relays))
            await asyncio.sleep(0)

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content)["session_id"])
        return httpx.Response(201, json={})

    async with _recover(app, monkeypatch, respond) as (relays, finished):
        ticker = asyncio.create_task(heartbeat())
        try:
            await asyncio.wait_for(finished.wait(), budget(5))
        finally:
            stop.set()
            await ticker
            monkeypatch.setattr(registry, "get", registry_get)
        assert relays[0] == parent.id
        assert not set(mirrors).intersection(sid for sid, _ in reads)
        assert all(thread != loop_thread for _, thread in reads)
        assert all(0 < len(ids) <= batch_size for ids in attachment_batches)
        assert any(1 < count <= batch_size + 1 for count in heartbeats)
        if superseded:
            assert len(attachment_batches) == 2
            assert set(relays) == {parent.id, *attachment_batches[0]}
            assert requests == []
            assert sessions._session_status_cache[parent.id] == "failed"
        else:
            assert len(attachment_batches) == 3
            assert set().union(*attachment_batches) == set(mirrors)
            assert set(mirrors) <= set(relays)
            revalidated = [ids for ids in batches if parent.id in ids and set(mirrors) & ids]
            assert revalidated == [{parent.id, *mirrors}]
            assert requests == [parent.id]
            assert sessions._session_status_cache[parent.id] == "idle"


async def test_failed_root_init_keeps_failure_but_does_not_block_other_tree(
    app: FastAPI, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    failed, healthy = [_create_session(app, agent.id) for _ in range(2)]
    blocked_child = _create_session(
        app, agent.id, kind="sub_agent", parent_conversation_id=failed.id
    )
    requested = []

    async def respond(request: httpx.Request) -> httpx.Response:
        sid = json.loads(request.content)["session_id"]
        requested.append(sid)
        return httpx.Response(503 if sid == failed.id else 201, json={})

    async with _recover(app, monkeypatch, respond) as (relays, finished):
        await asyncio.wait_for(finished.wait(), budget(5))
        assert {failed.id, healthy.id, blocked_child.id} <= set(relays)
        assert sessions._session_status_cache[failed.id] == "failed"
        assert sessions._session_status_cache[healthy.id] == "idle"
        assert set(requested) == {failed.id, healthy.id}


async def test_failed_binding_batch_does_not_strand_later_roots(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlalchemy.exc import OperationalError

    import omnigent.server.app as server_app

    monkeypatch.setattr(server_app, "_RECONNECT_BINDING_BATCH_SIZE", 1)
    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    failed, healthy = [_create_session(app, agent.id) for _ in range(2)]
    store = app.state.runner_router._conversation_store
    get_many = store.get_conversations
    list_bound = store.list_conversations_by_runner_id
    failure = OperationalError("binding lookup", {}, RuntimeError("database unavailable"))

    def failed_batch_first(runner_id: str) -> list[Conversation]:
        return sorted(list_bound(runner_id), key=lambda row: row.id != failed.id)

    def read_bindings(ids: list[str]) -> dict[str, Conversation]:
        if failed.id in ids:
            raise failure
        return get_many(ids)

    monkeypatch.setattr(store, "get_conversations", read_bindings)
    monkeypatch.setattr(store, "list_conversations_by_runner_id", failed_batch_first)
    requested: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["session_id"])
        return httpx.Response(201)

    async with _recover(app, monkeypatch, respond) as (relays, finished):
        await asyncio.wait_for(finished.wait(), budget(5))
        assert relays == requested == [healthy.id]
        assert sessions._session_status_cache[healthy.id] == "idle"
        assert sessions._session_status_cache[failed.id] == "failed"
        assert any(
            record.levelno == logging.ERROR
            and "Failed to refresh session bindings" in record.getMessage()
            and record.exc_info is not None
            and record.exc_info[1] is failure
            for record in caplog.records
        )


@pytest.mark.parametrize("phase", ["listing", "attachment"])
@pytest.mark.parametrize("dependent_kind", [None, "child", "mirror"])
async def test_reconnect_skips_root_rebound_before_initialization(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    dependent_kind: str | None,
) -> None:
    import omnigent.server.app as server_app

    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    moved, healthy = [_create_session(app, agent.id) for _ in range(2)]
    store = app.state.runner_router._conversation_store
    dependent = None
    if dependent_kind is not None:
        dependent = _create_session(
            app, agent.id, kind="sub_agent", parent_conversation_id=moved.id
        )
        if dependent_kind == "mirror":
            store.set_labels(dependent.id, {"omnigent.wrapper": "codex-native-ui-subagent"})
    moved_ids = [moved.id, *([dependent.id] if dependent is not None else [])]
    relay_bindings: dict[str, str] = {}
    requested: list[str] = []

    def rebind_subtree() -> None:
        for sid in moved_ids:
            store.replace_runner_id(sid, "destination-runner")
            relay_bindings[sid] = "destination-runner"

    async def rebind_after_listing(call: Any, *args: Any, **kwargs: Any) -> Any:
        result = await asyncio.to_thread(call, *args, **kwargs)
        if (
            phase == "listing"
            and call == store.list_conversations_by_runner_id
            and any(row.id == moved.id for row in result)
        ):
            await asyncio.to_thread(rebind_subtree)
        return result

    _patch_to_thread(monkeypatch, server_app, rebind_after_listing)

    async def respond(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["session_id"])
        return httpx.Response(201)

    try:
        async with _recover(app, monkeypatch, respond) as (relays, finished):
            original_relay = sessions._ensure_runner_relay

            def attach_then_rebind(sid: str, rid: str, *args: Any, **kwargs: Any) -> None:
                original_relay(sid, rid, *args, **kwargs)
                relay_bindings[sid] = rid
                if phase == "attachment" and sid == moved.id:
                    rebind_subtree()

            monkeypatch.setattr(sessions, "_ensure_runner_relay", attach_then_rebind)
            await asyncio.wait_for(finished.wait(), budget(5))
            assert requested == [healthy.id]
            assert (moved.id in relays) == (phase == "attachment")
            if dependent is not None:
                assert dependent.id not in relays
            assert all(relay_bindings[sid] == "destination-runner" for sid in moved_ids)
            assert sessions._session_status_cache[moved.id] == "failed"
            current = store.get_conversation(moved.id)
            assert current is not None
            assert current.runner_id == "destination-runner"
            assert current.labels["omnigent.last_task_error_code"] == "runner_disconnected"
    finally:
        for sid in moved_ids:
            sessions._session_status_cache.pop(sid, None)


async def test_reconnecting_trees_share_store_budget_without_waiting_for_initialization(
    app: FastAPI, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.server.app as server_app
    from omnigent.server import child_session_recovery, runner_session_init
    from omnigent.server.routes._sessions import helpers, orchestration

    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    roots = [_create_session(app, agent.id) for _ in range(12)]
    children = [
        _create_session(app, agent.id, kind="sub_agent", parent_conversation_id=root.id)
        for root in roots
    ]
    hung_ids = {child.id for child in children[:-1]}
    expected = {row.id for row in roots + children}
    requested: set[str] = set()
    attachment_sessions: set[str] = set()
    all_requested = asyncio.Event()
    pending_reads = peak_reads = 0

    async def tracked_store_call(call: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal pending_reads, peak_reads
        pending_reads += 1
        peak_reads = max(peak_reads, pending_reads)
        if call.__name__ == "_filesystem_attachment_in_history":
            attachment_sessions.add(args[0])
        try:
            return await asyncio.to_thread(call, *args, **kwargs)
        finally:
            pending_reads -= 1

    for module in (
        server_app,
        child_session_recovery,
        runner_session_init,
        helpers,
        orchestration,
    ):
        _patch_to_thread(monkeypatch, module, tracked_store_call)

    async def respond(request: httpx.Request) -> httpx.Response:
        sid = json.loads(request.content)["session_id"]
        requested.add(sid)
        if requested == expected:
            all_requested.set()
        if sid in hung_ids:
            await asyncio.Event().wait()
        return httpx.Response(201)

    async with _recover(app, monkeypatch, respond) as (relays, finished):
        await asyncio.wait_for(all_requested.wait(), budget(5))
        assert requested == expected
        assert attachment_sessions == expected
        assert expected <= set(relays)
        assert 1 < peak_reads <= RECOVERY_STORE_CONCURRENCY
        assert not finished.is_set()


async def test_hosted_child_recovers_independently_while_parent_is_stalled(
    app: FastAPI, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    parent = _create_session(app, agent.id)
    hostless, hosted = [
        _create_session(app, agent.id, kind="sub_agent", parent_conversation_id=parent.id)
        for _ in range(2)
    ]
    store = app.state.runner_router._conversation_store
    store.set_host_id(hosted.id, "a" * 32, workspace="/tmp")
    requested: set[str] = set()
    parent_entered = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        sid = json.loads(request.content)["session_id"]
        requested.add(sid)
        if sid == parent.id:
            parent_entered.set()
            await asyncio.Event().wait()
        return httpx.Response(201)

    async def hosted_recovers() -> None:
        while sessions._session_status_cache.get(hosted.id) != "idle":
            await asyncio.sleep(0)

    async with _recover(app, monkeypatch, respond) as (relays, finished):
        await asyncio.wait_for(asyncio.gather(parent_entered.wait(), hosted_recovers()), budget(5))
        assert requested == {parent.id, hosted.id}
        assert {parent.id, hostless.id, hosted.id} <= set(relays)
        assert sessions._session_status_cache[parent.id] == "failed"
        assert sessions._session_status_cache[hostless.id] == "failed"
        assert not finished.is_set()


@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, RuntimeError])
async def test_root_recovery_failure_is_quiet_only_after_tunnel_replacement(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    superseded: bool,
    error_type: type[Exception],
) -> None:
    agent = SqlAlchemyAgentStore(db_uri).create(generate_agent_id(), "test", "bundle")
    root = _create_session(app, agent.id)
    caplog.set_level(logging.INFO, logger="omnigent.server.app")

    async def respond(_: httpx.Request) -> httpx.Response:
        if superseded:
            monkeypatch.setattr(app.state.tunnel_registry, "get", lambda _: None)
        raise error_type("runner recovery failed during initialization")

    async with _recover(app, monkeypatch, respond) as (_, finished):
        await asyncio.wait_for(finished.wait(), budget(5))
        assert sessions._session_status_cache[root.id] == "failed"
        errors = [
            record
            for record in caplog.records
            if record.name == "omnigent.server.app" and record.levelno >= logging.ERROR
        ]
        assert bool(errors) is not superseded
        if superseded:
            assert any(
                "Stopped recovering session" in record.getMessage() for record in caplog.records
            )
