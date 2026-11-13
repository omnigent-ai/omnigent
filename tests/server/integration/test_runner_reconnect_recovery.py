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
from omnigent.entities import Agent, Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_MCP_REGISTRY, CAP_TCP_EGRESS
from omnigent.runtime import get_artifact_store
from omnigent.server.bundles import bundle_location
from omnigent.server.child_session_recovery import RECOVERY_STORE_CONCURRENCY
from omnigent.server.routes import runner_tunnel, sessions
from tests.budgets import budget
from tests.server.helpers import build_agent_bundle
from tests.server.integration.test_runner_tunnel_route import (
    _RUNNER_ID,
    _TUNNEL_PATH,
    _connect_route,
    _send_hello,
)
from tests.server.integration.test_sessions_permissions import _end_stream_via_close

pytestmark = pytest.mark.asyncio


@pytest.fixture
def agent(app: FastAPI) -> Agent:
    agent_id = generate_agent_id()
    bundle = build_agent_bundle("test")
    location = bundle_location(agent_id, bundle)
    get_artifact_store().put(location, bundle)
    return app.state.agent_store.create(agent_id, "test", location)


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
    *,
    capabilities: list[str] | None = None,
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
            if capabilities is not None:
                connection = app.state.tunnel_registry.get(_RUNNER_ID)
                assert connection is not None
                connection.hello.capabilities[:] = capabilities
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
    app: FastAPI, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
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


@pytest.mark.parametrize("cleanup_stage", ["route", "initializer"])
async def test_replaced_tunnel_cleanup_cannot_settle_a_newer_outage_as_idle(
    app: FastAPI,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup_stage: str,
) -> None:
    """A retired connection's delayed teardown cannot attest a newer turn was idle."""
    from omnigent.runner.transports.ws_tunnel.frames import RUNNER_IDLE_SHUTDOWN_CLOSE_REASON

    registry = app.state.tunnel_registry
    store = app.state.runner_router._conversation_store
    first_ready, second_ready = asyncio.Event(), asyncio.Event()
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
    real_hook = runner_tunnel._run_connect_hook

    async def delayed_cleanup(hook: Any, connection: Any) -> None:
        await real_hook(hook, connection)
        if not first_ready.is_set():
            first_ready.set()
            if cleanup_stage == "route":
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await release_cleanup.wait()
        else:
            second_ready.set()

    monkeypatch.setattr(runner_tunnel, "_run_connect_hook", delayed_cleanup)
    monkeypatch.setattr(sessions, "RUNNER_DISCONNECT_GRACE_S", 0.5)
    first = await _connect_route(app, _TUNNEL_PATH)
    second = None
    conv = None
    init_task = None
    init_client = None
    try:
        await _send_hello(first, registry, connection_id="old-idle-tunnel")
        await asyncio.wait_for(first_ready.wait(), budget(2))
        if cleanup_stage == "initializer":
            init_started = asyncio.Event()

            async def delayed_runner_request(_: httpx.Request) -> httpx.Response:
                init_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await release_cleanup.wait()
                return httpx.Response(201)

            init_client = httpx.AsyncClient(
                transport=httpx.MockTransport(delayed_runner_request), base_url="http://runner"
            )
            init_conv = store.create_conversation(agent_id=agent.id, runner_id=_RUNNER_ID)
            init_task = asyncio.create_task(
                app.state.runner_session_initializer.initialize(
                    init_conv, init_client, timeout=budget(5)
                )
            )
            await asyncio.wait_for(init_started.wait(), budget(2))
        await first.send_input(
            {
                "type": "websocket.disconnect",
                "code": 1000,
                "reason": RUNNER_IDLE_SHUTDOWN_CLOSE_REASON,
            }
        )
        await asyncio.wait_for(cleanup_started.wait(), budget(2))
        if cleanup_stage == "initializer":
            assert registry.get(_RUNNER_ID) is None
            # Remove the old session so the replacement only owns the new turn below.
            await store.delete_conversation(init_conv.id)
        second = await _connect_route(app, _TUNNEL_PATH)
        await _send_hello(second, registry, connection_id="new-active-tunnel")
        await asyncio.wait_for(second_ready.wait(), budget(2))
        conv = store.create_conversation(agent_id=agent.id, runner_id=_RUNNER_ID)
        store.set_session_live_status(conv.id, "running")
        sessions._session_status_cache[conv.id] = "running"
        await second.send_input({"type": "websocket.disconnect", "code": 1006})
        await asyncio.wait_for(second.future, budget(2))
        grace_tasks = {
            task
            for task in asyncio.all_tasks()
            if task.get_name() == f"runner-disconnect-grace-{_RUNNER_ID}"
        }
        assert len(grace_tasks) == 1
        release_cleanup.set()
        await asyncio.wait_for(first.future, budget(2))
        if init_task is not None:
            with pytest.raises(ConnectionError, match="initialization was cancelled"):
                await init_task
        assert grace_tasks == {
            task
            for task in asyncio.all_tasks()
            if task.get_name() == f"runner-disconnect-grace-{_RUNNER_ID}"
        }
        await asyncio.wait_for(asyncio.gather(*grace_tasks), budget(3))
        assert all(not task.cancelled() for task in grace_tasks)
        assert sessions._session_status_cache[conv.id] == "failed"
        final = store.get_conversation(conv.id)
        assert final is not None
        assert final.labels["omnigent.last_task_error_code"] == "runner_disconnected"
        failures = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "session_turn_failed"
            and getattr(record, "session_id", None) == conv.id
        ]
        assert len(failures) == 1
        assert failures[0].attributes["drop_close_code"] == "1006"
        assert failures[0].attributes["drop_close_reason_category"] == "abnormal"
    finally:
        release_cleanup.set()
        for communicator in (first, second):
            if communicator is not None and not communicator.future.done():
                await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
                await communicator.wait(timeout=budget(2))
        if init_task is not None:
            init_task.cancel()
            await asyncio.gather(init_task, return_exceptions=True)
        if init_client is not None:
            await init_client.aclose()
        pending_grace = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == f"runner-disconnect-grace-{_RUNNER_ID}"
        ]
        for task in pending_grace:
            task.cancel()
        await asyncio.gather(*pending_grace, return_exceptions=True)
        if conv is not None:
            sessions._session_status_cache.pop(conv.id, None)
            sessions._intentional_stop_sessions.pop(conv.id, None)


@pytest.mark.parametrize("superseded", [False, True])
async def test_large_mirror_tree_attaches_without_blocking_or_per_child_reads(
    app: FastAPI, agent: Agent, monkeypatch: pytest.MonkeyPatch, superseded: bool
) -> None:
    import omnigent.server.app as server_app

    parent = _create_session(app, agent.id)
    store = app.state.runner_router._conversation_store
    mirrors = []
    batch_size = server_app._RECONNECT_BINDING_BATCH_SIZE
    for _ in range(2 * batch_size + 1):
        child = _create_session(app, agent.id, kind="sub_agent", parent_conversation_id=parent.id)
        store.set_labels(child.id, {"omnigent.wrapper": "claude-code-native-ui-subagent"})
        mirrors.append(child.id)
    loop_thread = threading.get_ident()
    requirement_reads: list[int] = []

    def track_requirements(call: Any) -> Any:
        def tracked(*args: Any, **kwargs: Any) -> Any:
            requirement_reads.append(threading.get_ident())
            return call(*args, **kwargs)

        return tracked

    for owner, method in (
        (app.state.agent_store, "get"),
        (app.state.runner_router._agent_cache, "load"),
    ):
        monkeypatch.setattr(owner, method, track_requirements(getattr(owner, method)))
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

    async with _recover(app, monkeypatch, respond, capabilities=[CAP_MCP_REGISTRY]) as (
        relays,
        finished,
    ):
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
        assert requirement_reads and all(thread != loop_thread for thread in requirement_reads)
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
    app: FastAPI, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
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


@pytest.mark.parametrize("bundle_state", ["missing", "invalid", "downgraded"])
async def test_unreadable_bundle_does_not_block_other_sessions_on_old_runner(
    app: FastAPI,
    client: httpx.AsyncClient,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bundle_state: str,
) -> None:
    broken_id = generate_agent_id()
    bundle = (
        build_agent_bundle(
            "restricted",
            os_env={
                "type": "caller_process",
                "sandbox": {
                    "type": "linux_bwrap",
                    "tcp_egress": [{"name": "db", "host": "db.example.test", "port": 5432}],
                },
            },
        )
        if bundle_state == "downgraded"
        else b"invalid bundle"
    )
    location = bundle_location(broken_id, bundle)
    if bundle_state != "missing":
        get_artifact_store().put(location, bundle)
    app.state.agent_store.create(broken_id, "broken", location)
    failed = _create_session(app, broken_id)
    healthy = _create_session(app, agent.id)
    router = app.state.runner_router
    from omnigent.runtime import _globals

    monkeypatch.setattr(_globals, "_runner_router", router)
    store = router._conversation_store
    list_bound = store.list_conversations_by_runner_id
    monkeypatch.setattr(
        store,
        "list_conversations_by_runner_id",
        lambda rid: sorted(list_bound(rid), key=lambda row: row.id != failed.id),
    )
    requested: list[str] = []
    controls: list[str] = []
    deletions: list[str] = []
    reads: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            reads.append(request.url.path)
            terminal = {"id": "shell", "type": "terminal"}
            return httpx.Response(
                200,
                json={"data": [terminal]} if request.url.path.endswith("/terminals") else terminal,
            )
        if request.method == "DELETE":
            deletions.append(request.url.path)
            return httpx.Response(200, json={"deleted": True})
        if request.url.path.endswith("/events"):
            controls.append(json.loads(request.content)["type"])
            return httpx.Response(200, json={"queued": True})
        requested.append(json.loads(request.content)["session_id"])
        return httpx.Response(201)

    async with _recover(app, monkeypatch, respond, capabilities=[CAP_MCP_REGISTRY]) as (
        relays,
        finished,
    ):
        connection = app.state.tunnel_registry.get(_RUNNER_ID)
        assert connection is not None and CAP_TCP_EGRESS not in connection.hello.capabilities
        for lookup in (
            lambda: router.client_for_conversation(
                conversation_id=failed.id, harness="claude-sdk"
            ),
            lambda: router.client_for_session_resources(failed.id),
            lambda: router.client_for_existing_conversation(failed.id),
        ):
            with pytest.raises(OmnigentError) as error:
                await asyncio.to_thread(lookup)
            assert error.value.code == (
                ErrorCode.RUNNER_CAPABILITY_MISMATCH
                if bundle_state == "downgraded"
                else ErrorCode.CONFLICT
            )
        await asyncio.wait_for(finished.wait(), budget(5))
        assert relays == requested == [healthy.id]
        assert sessions._session_status_cache[healthy.id] == "idle"
        assert sessions._session_status_cache[failed.id] == "failed"
        assert any(
            "Failed to resolve runner client" in record.getMessage()
            and record.exc_info is not None
            and isinstance(record.exc_info[1], OmnigentError)
            for record in caplog.records
        )
        for event_type in ("interrupt", "stop_session"):
            response = await client.post(
                f"/v1/sessions/{failed.id}/events", json={"type": event_type}
            )
            assert response.status_code == 202, response.text
        assert controls == ["interrupt", "stop_session"]
        assert requested == [healthy.id]
        assert store.get_conversation(failed.id).host_id is None
        from omnigent.server.routes._sessions.helpers import (
            _forward_approval_to_runner,
            _forward_session_change_to_runner_impl,
        )

        await _forward_session_change_to_runner_impl(failed.id, router, {"type": "interrupt"})
        assert controls == ["interrupt", "stop_session", "interrupt"]
        for action in ("decline", "cancel"):
            await _forward_approval_to_runner(failed.id, {"action": action}, router)
        assert controls[-2:] == ["approval", "approval"]
        for action in ("accept", [], {}):
            with pytest.raises(OmnigentError):
                await _forward_approval_to_runner(failed.id, {"action": action}, router)
        for event in (
            {"type": "effort_change", "effort": "high"},
            {"type": "model_change", "model": "example-model"},
        ):
            assert await _forward_session_change_to_runner_impl(failed.id, router, event) is None
        for event_type in ("cost_approval_popup", "policy_blocked_notice"):
            await _forward_session_change_to_runner_impl(failed.id, router, {"type": event_type})
        assert controls[-2:] == ["cost_approval_popup", "policy_blocked_notice"]
        with pytest.raises(OmnigentError):
            await _forward_session_change_to_runner_impl(failed.id, router, {"type": "compact"})

        async def relay_ready(_session_id, _runner_id, runner_client, *_args, **_kwargs):
            assert runner_client is not None

        monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", relay_ready)
        stream_task = asyncio.create_task(client.get(f"/v1/sessions/{failed.id}/stream"))
        try:
            response = await _end_stream_via_close(failed.id, stream_task)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
            first_data = next(
                line[6:] for line in response.text.splitlines() if line.startswith("data: ")
            )
            assert json.loads(first_data)["type"] == "session.heartbeat"
        finally:
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
        terminal_path = f"/v1/sessions/{failed.id}/resources/terminals/shell"
        for path in (terminal_path.rsplit("/", 1)[0], terminal_path):
            response = await client.get(path)
            assert response.status_code == 200, response.text
            assert path in reads
            payload = response.json()
            terminal = payload["data"][0] if "data" in payload else payload
            assert terminal["id"] == "shell"
            assert "direct_attach_url" not in terminal.get("metadata", {})
        response = await client.delete(terminal_path)
        assert response.status_code == 200, response.text
        response = await client.delete(f"/v1/sessions/{failed.id}")
        assert response.status_code == 200, response.text
        assert terminal_path in deletions and f"/v1/sessions/{failed.id}" in deletions
        assert store.get_conversation(failed.id) is None
        assert requested == [healthy.id]


async def test_failed_binding_batch_does_not_strand_later_roots(
    app: FastAPI,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlalchemy.exc import OperationalError

    import omnigent.server.app as server_app

    monkeypatch.setattr(server_app, "_RECONNECT_BINDING_BATCH_SIZE", 1)
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
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    dependent_kind: str | None,
) -> None:
    import omnigent.server.app as server_app

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
    app: FastAPI, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.server.app as server_app
    from omnigent.server import child_session_recovery, runner_session_init
    from omnigent.server.routes._sessions import helpers, orchestration

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
    app: FastAPI, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    superseded: bool,
    error_type: type[Exception],
) -> None:
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
        transport_warnings = [
            record
            for record in caplog.records
            if record.name == "omnigent.server.app"
            and record.levelno == logging.WARNING
            and "Lost runner tunnel" in record.getMessage()
        ]
        if superseded:
            assert not errors
            assert any(
                "Stopped recovering session" in record.getMessage() for record in caplog.records
            )
        elif error_type is httpx.ConnectError:
            # A transport loss on the live tunnel is retried by the next reconnect.
            assert not errors
            assert transport_warnings
        else:
            assert errors
