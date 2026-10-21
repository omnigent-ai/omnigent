"""Model picks must wait for or recover Codex runtime readiness on a connected runner."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from websockets.asyncio.server import Server, ServerConnection, serve

from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id
from omnigent.harnesses.codex_native import app_server, bridge
from omnigent.native import native_cost_popup
from omnigent.runner import native_controls
from omnigent.runner.native import orchestration as native_runtime
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runtime import session_stream
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.terminals.pane_reaper import NativePaneReaper
from omnigent.terminals.registry import TerminalRegistry
from tests._helpers.polling import poll_until_async
from tests.budgets import budget
from tests.runner.conftest import (
    _build_app_for_spec,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import make_test_terminal_instance
from tests.runner.native_helpers import _harness_spec
from tests.server.helpers import create_test_agent

_PREVIOUS_MODEL = "gpt-5.4"
_REQUESTED_MODEL = "gpt-6-sol"
_LATEST_MODEL = "gpt-6-astra"


@dataclass
class _LoopbackCodex:
    """Minimal Codex RPC server whose listener is closed by the real idle reaper."""

    bridge_dir: Path
    session_id: str
    server: Server | None = None
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    launches: int = 0
    stops: int = 0
    policy_notice_pending: bool = False
    thread_id: str = "thread_idle_reap"
    transport: str = ""
    ready_gate: asyncio.Event | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    startup_task: asyncio.Task[None] | None = None
    saved_threads: dict[str, dict[str, str]] = field(default_factory=dict)
    loaded_threads: dict[str, dict[str, str]] = field(default_factory=dict)
    applied_updates: list[dict[str, Any]] = field(default_factory=list)
    rpc_errors: list[dict[str, Any]] = field(default_factory=list)
    response_errors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    refuse_handshake: bool = False
    proc: SimpleNamespace | None = None

    async def start(self) -> None:
        assert self.server is None
        bridge.clear_bridge_state(self.bridge_dir)
        self.ready.clear()
        self.proc = SimpleNamespace(returncode=None)
        self.server = await serve(
            self.handle,
            "127.0.0.1",
            0,
            process_request=lambda connection, _request: (
                connection.respond(403, "Forbidden") if self.refuse_handshake else None
            ),
        )
        port = self.server.sockets[0].getsockname()[1]
        self.transport = f"ws://127.0.0.1:{port}"
        self.launches += 1
        if self.ready_gate is None:
            self.publish_ready()
        else:
            self.startup_task = asyncio.create_task(self._finish_startup())

    async def _finish_startup(self) -> None:
        assert self.ready_gate is not None
        await self.ready_gate.wait()
        self.publish_ready()

    def load_thread(self, thread_id: str) -> dict[str, str]:
        saved = self.saved_threads.setdefault(
            thread_id,
            {
                "model": bridge.read_codex_config_model(self.bridge_dir) or _PREVIOUS_MODEL,
                "effort": bridge.read_codex_config_effort(self.bridge_dir) or "high",
            },
        )
        return self.loaded_threads.setdefault(thread_id, dict(saved))

    def publish_ready(self) -> None:
        self.load_thread(self.thread_id)
        bridge.write_bridge_state(
            self.bridge_dir,
            bridge.CodexNativeBridgeState(
                session_id=self.session_id,
                thread_id=self.thread_id,
                socket_path=self.transport,
                codex_home=str(bridge.codex_home_for_bridge_dir(self.bridge_dir)),
            ),
        )
        self.ready.set()

    async def close(self) -> None:
        if self.proc is not None:
            self.proc.returncode = 0
        if self.startup_task is not None:
            self.startup_task.cancel()
            await asyncio.gather(self.startup_task, return_exceptions=True)
            self.startup_task = None
        self.loaded_threads.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            self.stops += 1

    async def handle(self, websocket: ServerConnection) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            method = message["method"]
            params = message.get("params", {})
            self.requests.append((method, params))
            if method == "initialized":
                continue
            errors = self.response_errors.get(method)
            if errors:
                error = errors.pop(0)
                self.rpc_errors.append(error)
                await websocket.send(json.dumps({"id": message["id"], "error": error}))
                continue
            result: dict[str, Any] = {}
            if method == "model/list":
                result = {
                    "data": [
                        {
                            "id": model,
                            "defaultReasoningEffort": "high",
                            "supportedReasoningEfforts": [
                                {"reasoningEffort": effort} for effort in ("low", "high")
                            ],
                        }
                        for model in (_PREVIOUS_MODEL, _REQUESTED_MODEL, _LATEST_MODEL)
                    ],
                    "nextCursor": None,
                }
            elif method == "thread/settings/update":
                thread_id = params["threadId"]
                if thread_id not in self.loaded_threads:
                    error = {"code": -32600, "message": f"thread not found: {thread_id}"}
                    self.rpc_errors.append(error)
                    await websocket.send(json.dumps({"id": message["id"], "error": error}))
                    continue
                settings = {key: params[key] for key in ("model", "effort") if key in params}
                self.loaded_threads[thread_id].update(settings)
                self.saved_threads[thread_id].update(settings)
                self.applied_updates.append(params)
            elif method == "thread/resume":
                self.load_thread(params["threadId"])
                result = {"thread": {"id": params["threadId"]}}
            elif method == "thread/list/loaded":
                result = {"data": list(self.loaded_threads), "nextCursor": None}
            else:
                assert method == "initialize"
            await websocket.send(json.dumps({"id": message["id"], "result": result}))


@dataclass
class _NativeSession:
    session_id: str
    store: SqlAlchemyConversationStore
    runner: httpx.AsyncClient
    process_manager: _FakeProcessManager
    terminals: TerminalRegistry
    resources: SessionResourceRegistry
    reaper: NativePaneReaper
    codex: _LoopbackCodex
    events: list[dict[str, Any]]
    runner_replies: list[httpx.Response]

    async def reap(self, *, clear_bridge: bool) -> None:
        assert self.terminals.get(self.session_id, "codex", "main") is not None
        self.reaper._last_busy_at[self.session_id] = time.monotonic() - 3601
        await self.reaper._scan_once()
        assert self.terminals.get(self.session_id, "codex", "main") is None
        assert self.session_id not in native_runtime._AUTO_CODEX_APP_SERVERS
        assert self.codex.stops == 1
        assert self.codex.server is None
        assert bridge.read_bridge_state(self.codex.bridge_dir) is not None
        if clear_bridge:
            bridge.clear_bridge_state(self.codex.bridge_dir)
        health = await self.runner.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

    async def reopen_terminal(self) -> None:
        response = await self.runner.post(
            f"/v1/sessions/{self.session_id}/resources/terminals",
            json={
                "terminal": "codex",
                "session_key": "main",
                "ensure_native_terminal": True,
                "persist_resource_event": False,
            },
        )
        assert response.status_code == 200, response.text

    def assert_settings(self, model: str, effort: str = "high") -> None:
        saved = self.store.get_conversation(self.session_id)
        assert saved.model_override == model
        assert saved.reasoning_effort == effort
        assert bridge.read_codex_config_model(self.codex.bridge_dir) == model
        assert bridge.read_codex_config_effort(self.codex.bridge_dir) == effort
        assert self.codex.loaded_threads[self.codex.thread_id] == {
            "model": model,
            "effort": effort,
        }

    def assert_applied(
        self, response: httpx.Response, *, model: str = _REQUESTED_MODEL, effort: str = "high"
    ) -> None:
        assert response.status_code == 200, response.text
        assert response.json()["model_override"] == model
        self.assert_settings(model, effort)
        assert self.codex.applied_updates == [
            {"threadId": self.codex.thread_id, "model": model, "effort": effort}
        ]
        assert not any(event.get("type") == "response.error" for event in self.events)


@pytest.fixture
async def native_session(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[_NativeSession]:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "labels": {"omnigent.wrapper": "codex-native-ui"},
            "model_override": _PREVIOUS_MODEL,
            "reasoning_effort": "high",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    host = HostStore(db_uri).upsert_on_connect(
        "6b9c07bfb42f687d53af44f018adebec", "laptop", "owner@example.com"
    )
    store.set_host_id(session_id, host_id=host.host_id, workspace=str(tmp_path))
    store.replace_runner_id(session_id, "runner_connected")

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    monkeypatch.setattr(app_server, "_effort_catalog_cache", {})
    monkeypatch.setattr(native_runtime, "_AUTO_CODEX_APP_SERVERS", {})
    monkeypatch.setattr(native_runtime, "_AUTO_FORWARDER_TASKS", {})
    monkeypatch.setattr(native_cost_popup, "_tmux_last_client_input_at", lambda *_args: None)
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", lambda *_args: None)
    bridge_dir = bridge.bridge_dir_for_bridge_id(session_id)
    codex_home = bridge.codex_home_for_bridge_dir(bridge_dir)
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        f'model = "{_PREVIOUS_MODEL}"\nmodel_reasoning_effort = "high"\n'
    )
    codex = _LoopbackCodex(bridge_dir, session_id)
    if getattr(request, "param", None) == "starting":
        codex.ready_gate = asyncio.Event()
    terminals = TerminalRegistry()

    async def launch(
        sid: str, resources: SessionResourceRegistry, *_args: Any, **_kwargs: Any
    ) -> SessionResourceView:
        assert sid == session_id
        await codex.start()
        pane = make_test_terminal_instance("codex", "main", tmp_path)
        monkeypatch.setattr(pane, "_tmux", AsyncMock())
        terminals._by_conversation[sid] = {("codex", "main"): pane}
        terminal_id = terminal_resource_id("codex", "main")
        resources._terminal_roles[(sid, terminal_id)] = "codex-native"
        native_runtime._AUTO_CODEX_APP_SERVERS[sid] = codex  # type: ignore[assignment]
        return SessionResourceView(
            id=terminal_id, type="terminal", session_id=sid, name="codex:main"
        )

    monkeypatch.setattr(native_runtime, "_auto_create_codex_terminal", launch)
    runner_app, process_manager = await _build_app_for_spec(
        _harness_spec("codex-native", model=_PREVIOUS_MODEL), terminal_registry=terminals
    )
    reaper = runner_app.state.native_pane_reaper
    assert reaper is not None
    monkeypatch.setattr(reaper, "_idle_timeout_s", 3600)
    events: list[dict[str, Any]] = []
    publish = session_stream.publish

    def capture_event(sid: str, event: dict[str, Any]) -> None:
        if sid == session_id:
            events.append(event)
        publish(sid, event)

    monkeypatch.setattr(session_stream, "publish", capture_event)
    runner_replies: list[httpx.Response] = []

    async def capture_reply(response: httpx.Response) -> None:
        if response.request.url.path.endswith("/events"):
            await response.aread()
            runner_replies.append(response)

    try:
        async with _runner_client(runner_app) as runner:
            runner.event_hooks["response"].append(capture_reply)
            initialized = await runner.post(
                "/v1/sessions", json={"session_id": session_id, "agent_id": agent["id"]}
            )
            assert initialized.status_code == 201, initialized.text
            monkeypatch.setattr(
                "omnigent.server.routes.sessions._get_runner_client",
                AsyncMock(return_value=runner),
            )
            monkeypatch.setattr(
                routes_core,
                "ensure_runner_connected",
                AsyncMock(return_value=(runner, store.get_conversation(session_id))),
            )
            monkeypatch.setattr(routes_core, "_ensure_runner_relay_ready", AsyncMock())
            session = _NativeSession(
                session_id,
                store,
                runner,
                process_manager,
                terminals,
                runner_app.state.session_resource_registry,
                reaper,
                codex,
                events,
                runner_replies,
            )
            await session.reopen_terminal()
            assert codex.launches == 1
            yield session
    finally:
        await terminals.cleanup_conversation(session_id)
        await native_runtime.teardown_codex_native_app_server(session_id)
        await codex.close()


async def test_connected_codex_model_change_before_idle_reap(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    response = await client.patch(
        f"/v1/sessions/{native_session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    native_session.assert_applied(response)
    assert native_session.codex.launches == 1


@pytest.mark.parametrize("backend", ["running", "exited", "forwarder-ended", "unowned"])
async def test_new_turn_recovers_stopped_backend_behind_live_codex_pane(
    native_session: _NativeSession,
    backend: str,
) -> None:
    session = native_session
    pane = session.terminals.get(session.session_id, "codex", "main")
    assert pane is not None and await pane.is_alive()
    if backend != "running":
        await session.codex.close()
    if backend == "forwarder-ended":
        native_runtime._AUTO_CODEX_APP_SERVERS.pop(session.session_id)
        failure = bridge.CODEX_APP_SERVER_STOPPED
        bridge.write_bridge_startup_error(
            session.codex.bridge_dir, failure.message, code=failure.code
        )
    elif backend == "unowned":
        session.resources._terminal_roles.pop(
            (session.session_id, terminal_resource_id("codex", "main"))
        )
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_recovered"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_recovered"}}),
        ]
    )
    session.process_manager._client = harness

    response = await session.runner.post(
        f"/v1/sessions/{session.session_id}/events",
        json={
            "type": "message",
            "role": "user",
            "agent_id": session.store.get_conversation(session.session_id).agent_id,
            "content": [{"type": "input_text", "text": "Continue the session"}],
        },
    )
    assert response.status_code == 202, response.text
    await poll_until_async(lambda: harness.posted_bodies, timeout_s=budget(5))

    expected_launches = 2 if backend in {"exited", "forwarder-ended"} else 1
    assert session.codex.launches == expected_launches
    if backend in {"running", "unowned"}:
        assert session.terminals.get(session.session_id, "codex", "main") is pane
    else:
        assert session.terminals.get(session.session_id, "codex", "main") is not pane
        assert bridge.read_bridge_startup_error(session.codex.bridge_dir) is None
        state = bridge.read_bridge_state(session.codex.bridge_dir)
        assert state is not None
        client = app_server.client_for_transport(state.socket_path, client_name="recovery-test")
        try:
            await client.connect()
            loaded = await client.request("thread/list/loaded", {})
            assert loaded["result"]["data"] == [session.codex.thread_id]
        finally:
            await client.close()


@pytest.mark.parametrize("combined", [False, True], ids=["model", "model-and-effort"])
async def test_model_change_recovers_dead_backend_while_codex_pane_is_alive(
    client: httpx.AsyncClient, native_session: _NativeSession, combined: bool
) -> None:
    session = native_session
    pane = session.terminals.get(session.session_id, "codex", "main")
    assert pane is not None and await pane.is_alive()
    await session.codex.close()
    assert native_runtime._AUTO_CODEX_APP_SERVERS[session.session_id] is session.codex
    assert bridge.read_bridge_state(session.codex.bridge_dir) is not None
    selection = {"model_override": _REQUESTED_MODEL}
    if combined:
        selection["reasoning_effort"] = "low"

    response = await asyncio.wait_for(
        client.patch(f"/v1/sessions/{session.session_id}", json=selection), timeout=budget(5)
    )

    session.assert_applied(response, effort="low" if combined else "high")
    assert session.codex.launches == 2


async def test_codex_listener_refusal_preserves_a_live_backend_connection(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    session = native_session
    accepted = app_server.CodexAppServerClient(ws_url=session.codex.transport)
    await accepted.connect()
    server = session.codex.server
    assert server is not None
    server.close(close_connections=False)
    while server.is_serving():
        await asyncio.sleep(0)

    try:
        response = await client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
        )
        _assert_codex_refusal(session, response)
        assert session.codex.stops == 0
        assert session.codex.launches == 1
        result = await accepted.request("thread/list/loaded", {})
        assert result["result"] == {
            "data": [session.codex.thread_id],
            "nextCursor": None,
        }
    finally:
        await accepted.close()


async def test_codex_refused_connection_does_not_close_a_replacement_backend(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    await session.codex.close()
    replacement = SimpleNamespace(
        bridge_dir=session.codex.bridge_dir,
        proc=SimpleNamespace(returncode=0),
        close=AsyncMock(),
    )
    connect = app_server.CodexAppServerClient.connect
    replaced = False

    async def replace_before_refusal(rpc: app_server.CodexAppServerClient) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            native_runtime._AUTO_CODEX_APP_SERVERS[session.session_id] = replacement
            raise ConnectionRefusedError("The old listener disappeared during replacement")
        await connect(rpc)

    monkeypatch.setattr(app_server.CodexAppServerClient, "connect", replace_before_refusal)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    _assert_codex_refusal(session, response)
    replacement.close.assert_not_awaited()
    assert native_runtime._AUTO_CODEX_APP_SERVERS[session.session_id] is replacement
    assert session.codex.launches == 2


async def test_codex_unregistered_backend_is_not_restarted_after_connection_refusal(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    session = native_session
    await session.codex.close()
    native_runtime._AUTO_CODEX_APP_SERVERS.pop(session.session_id)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    _assert_codex_refusal(session, response)
    assert session.codex.launches == 1
    assert session.session_id not in native_runtime._AUTO_CODEX_APP_SERVERS


async def test_codex_live_handshake_refusal_does_not_restart_backend(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    session = native_session
    session.codex.refuse_handshake = True
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    _assert_codex_refusal(session, response)
    assert session.codex.launches == 1
    assert session.codex.stops == 0
    assert session.codex.requests == []

    session.codex.refuse_handshake = False
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    assert response.status_code == 200, response.text
    session.assert_settings(_REQUESTED_MODEL)
    assert len(session.codex.applied_updates) == 1
    assert session.codex.launches == 1


async def test_codex_connection_refused_after_settings_send_is_not_replayed(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    request = app_server.CodexAppServerClient.request

    async def lose_reply(
        rpc: app_server.CodexAppServerClient, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        result = await request(rpc, method, params)
        if method == "thread/settings/update":
            raise ConnectionRefusedError("Settings reply was lost")
        return result

    monkeypatch.setattr(app_server.CodexAppServerClient, "request", lose_reply)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    assert response.status_code == 503, response.text
    assert len(session.codex.applied_updates) == 1
    assert session.codex.launches == 1
    assert session.codex.stops == 0


async def test_unowned_codex_pane_is_not_restarted_after_connection_refusal(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    session = native_session
    await session.codex.close()
    session.resources._terminal_roles.pop(
        (session.session_id, terminal_resource_id("codex", "main"))
    )
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    _assert_codex_refusal(session, response)
    assert session.codex.launches == 1


async def test_codex_replacement_connection_refusal_is_not_retried_again(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    await session.codex.close()
    launch = native_runtime._auto_create_codex_terminal

    async def launch_without_listener(*args: Any, **kwargs: Any) -> SessionResourceView:
        resource = await launch(*args, **kwargs)
        await session.codex.close()
        return resource

    monkeypatch.setattr(native_runtime, "_auto_create_codex_terminal", launch_without_listener)
    response = await asyncio.wait_for(
        client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
        ),
        timeout=budget(5),
    )
    _assert_codex_refusal(session, response)
    assert session.codex.launches == 2
    assert session.codex.requests == []


async def test_codex_dead_backend_recovery_startup_timeout_refuses_settings(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    await session.codex.close()
    session.codex.ready_gate = asyncio.Event()
    monkeypatch.setattr(native_controls, "_CODEX_SETTINGS_READY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(native_controls, "_CODEX_SETTINGS_READY_POLL_S", 0.005)
    response = await asyncio.wait_for(
        client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
        ),
        timeout=budget(2),
    )
    _assert_codex_refusal(session, response)
    assert session.codex.launches == 2
    assert session.codex.requests == []


@pytest.mark.parametrize("clear_bridge", [False, True], ids=["stale-bridge", "missing-bridge"])
async def test_model_change_recovers_idle_reaped_codex_on_connected_runner(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    clear_bridge: bool,
) -> None:
    session = native_session
    await session.reap(clear_bridge=clear_bridge)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )

    session.assert_applied(response)
    assert session.codex.launches == 2


@pytest.mark.parametrize("clear_bridge", [False, True], ids=["stale-bridge", "missing-bridge"])
async def test_reopening_terminal_allows_model_change_after_idle_reap(
    client: httpx.AsyncClient, native_session: _NativeSession, clear_bridge: bool
) -> None:
    session = native_session
    await session.reap(clear_bridge=clear_bridge)
    await session.reopen_terminal()
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    session.assert_applied(response)
    assert session.codex.launches == 2


def _observe_missing_bridge(
    monkeypatch: pytest.MonkeyPatch, session: _NativeSession
) -> asyncio.Event:
    observed = asyncio.Event()
    loop = asyncio.get_running_loop()
    read_state = bridge.read_bridge_state

    def read(bridge_dir: Path) -> bridge.CodexNativeBridgeState | None:
        state = read_state(bridge_dir)
        if bridge_dir == session.codex.bridge_dir and state is None:
            loop.call_soon_threadsafe(observed.set)
        return state

    monkeypatch.setattr(bridge, "read_bridge_state", read)
    return observed


def _assert_codex_refusal(
    session: _NativeSession, response: httpx.Response, *, combined: bool = False
) -> None:
    assert response.status_code == 503, response.text
    assert session.runner_replies[0].status_code == 503
    assert session.runner_replies[0].json()["error"] == "codex_native_settings_update_failed"
    if combined:
        assert "model and reasoning effort changes" in response.text
        assert "previous selections have been restored" in response.text
    else:
        errors = [event["error"] for event in session.events if event["type"] == "response.error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "model_change_not_applied"
        assert _REQUESTED_MODEL in errors[0]["message"]
    saved = session.store.get_conversation(session.session_id)
    assert saved.model_override == _PREVIOUS_MODEL
    assert saved.reasoning_effort == "high"
    assert bridge.read_codex_config_model(session.codex.bridge_dir) == _PREVIOUS_MODEL
    assert bridge.read_codex_config_effort(session.codex.bridge_dir) == "high"
    assert session.codex.applied_updates == []


@pytest.mark.parametrize("native_session", ["starting"], indirect=True)
async def test_model_change_waits_for_codex_startup(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    assert session.codex.ready_gate is not None
    assert session.codex.server is not None
    assert session.terminals.get(session.session_id, "codex", "main") is not None
    assert not session.codex.ready.is_set()
    missing = _observe_missing_bridge(monkeypatch, session)
    change = asyncio.create_task(
        client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
        )
    )
    try:
        await asyncio.wait_for(missing.wait(), timeout=budget(5))
        assert session.codex.applied_updates == []
        session.codex.ready_gate.set()
        await asyncio.wait_for(session.codex.ready.wait(), timeout=budget(5))
        response = await asyncio.wait_for(change, timeout=budget(5))
    finally:
        session.codex.ready_gate.set()
        change.cancel()
        await asyncio.gather(change, return_exceptions=True)

    assert session.codex.launches == 1
    session.assert_applied(response)


@pytest.mark.parametrize("native_session", ["starting"], indirect=True)
async def test_model_change_succeeds_after_delayed_codex_startup(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    session = native_session
    assert session.codex.ready_gate is not None
    assert bridge.read_bridge_state(session.codex.bridge_dir) is None
    session.codex.ready_gate.set()
    await asyncio.wait_for(session.codex.ready.wait(), timeout=budget(5))
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    session.assert_applied(response)
    assert session.codex.launches == 1


@pytest.mark.parametrize("native_session", ["starting"], indirect=True)
@pytest.mark.parametrize("selection", ["model", "effort", "model-and-effort"])
async def test_codex_settings_forward_outlasts_tui_budget_during_startup(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    session = native_session
    assert session.codex.ready_gate is not None
    monkeypatch.setattr(routes_core, "_TUI_INJECT_FORWARD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(routes_core, "_CODEX_SETTINGS_FORWARD_TIMEOUT_S", 1.0)
    missing = _observe_missing_bridge(monkeypatch, session)
    post = session.runner.post

    async def timed_post(url: str, **kwargs: Any) -> httpx.Response:
        # ASGITransport doesn't enforce HTTP timeouts; use the actual forward
        # budget here so a premature public rollback is observable.
        try:
            return await asyncio.wait_for(post(url, **kwargs), timeout=kwargs["timeout"])
        except TimeoutError as exc:
            raise httpx.ReadTimeout("Runner response exceeded its forward budget") from exc

    monkeypatch.setattr(session.runner, "post", timed_post)
    settings = {}
    if "model" in selection:
        settings["model_override"] = _REQUESTED_MODEL
    if "effort" in selection:
        settings["reasoning_effort"] = "low"
    change = asyncio.create_task(client.patch(f"/v1/sessions/{session.session_id}", json=settings))
    try:
        await asyncio.wait_for(missing.wait(), timeout=budget(2))
        await asyncio.sleep(0.1)
        assert not change.done()
        session.codex.ready_gate.set()
        response = await asyncio.wait_for(change, timeout=budget(2))
    finally:
        session.codex.ready_gate.set()
        change.cancel()
        await asyncio.gather(change, return_exceptions=True)
    assert response.status_code == 200, response.text
    session.assert_settings(
        _REQUESTED_MODEL if "model" in selection else _PREVIOUS_MODEL,
        "low" if "effort" in selection else "high",
    )
    assert len(session.codex.applied_updates) == 1
    assert session.codex.launches == 1
    assert not any(event["type"] == "response.error" for event in session.events)


@pytest.mark.parametrize("native_session", ["starting"], indirect=True)
@pytest.mark.parametrize("combined", [False, True], ids=["model", "model-and-effort"])
async def test_codex_startup_timeout_refuses_settings_and_allows_retry(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    monkeypatch: pytest.MonkeyPatch,
    combined: bool,
) -> None:
    session = native_session
    assert session.codex.ready_gate is not None
    monkeypatch.setattr(native_controls, "_CODEX_SETTINGS_READY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(native_controls, "_CODEX_SETTINGS_READY_POLL_S", 0.005)
    selection = {"model_override": _REQUESTED_MODEL}
    if combined:
        selection["reasoning_effort"] = "low"
    response = await asyncio.wait_for(
        client.patch(f"/v1/sessions/{session.session_id}", json=selection), timeout=budget(2)
    )
    _assert_codex_refusal(session, response, combined=combined)
    assert session.codex.requests == []
    assert session.codex.launches == 1

    session.codex.ready_gate.set()
    await asyncio.wait_for(session.codex.ready.wait(), timeout=budget(2))
    response = await client.patch(f"/v1/sessions/{session.session_id}", json=selection)
    assert response.status_code == 200, response.text
    session.assert_settings(_REQUESTED_MODEL, "low" if combined else "high")
    assert len(session.codex.applied_updates) == 1
    assert session.codex.launches == 1


@pytest.mark.parametrize("native_session", ["starting"], indirect=True)
async def test_cancelling_codex_readiness_wait_releases_settings_lock(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    assert session.codex.ready_gate is not None
    missing = _observe_missing_bridge(monkeypatch, session)
    change = asyncio.create_task(
        session.runner.post(
            f"/v1/sessions/{session.session_id}/events",
            json={"type": "model_change", "model": _REQUESTED_MODEL, "rollback_on_refusal": True},
        )
    )
    try:
        await asyncio.wait_for(missing.wait(), timeout=budget(2))
    finally:
        change.cancel()
        await asyncio.gather(change, return_exceptions=True)
    assert change.cancelled()
    assert session.codex.applied_updates == []
    assert bridge.read_codex_config_model(session.codex.bridge_dir) == _PREVIOUS_MODEL

    session.codex.ready_gate.set()
    await asyncio.wait_for(session.codex.ready.wait(), timeout=budget(2))
    response = await asyncio.wait_for(
        client.patch(f"/v1/sessions/{session.session_id}", json={"model_override": _LATEST_MODEL}),
        timeout=budget(2),
    )
    session.assert_applied(response, model=_LATEST_MODEL)
    assert session.codex.launches == 1


async def test_codex_relaunch_failure_refuses_settings_and_allows_retry(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    await session.reap(clear_bridge=False)
    launch = native_runtime._auto_create_codex_terminal
    monkeypatch.setattr(
        native_runtime,
        "_auto_create_codex_terminal",
        AsyncMock(side_effect=RuntimeError("Codex failed to start")),
    )
    response = await asyncio.wait_for(
        client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
        ),
        timeout=budget(2),
    )
    _assert_codex_refusal(session, response)
    assert session.codex.requests == []
    assert session.codex.launches == 1

    monkeypatch.setattr(native_runtime, "_auto_create_codex_terminal", launch)
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    assert response.status_code == 200, response.text
    session.assert_settings(_REQUESTED_MODEL)
    assert len(session.codex.applied_updates) == 1
    assert session.codex.launches == 2


@pytest.mark.parametrize(
    "error",
    [
        {"code": -32600, "message": "model not found: gpt-6-sol"},
        {"code": -32603, "message": "thread not found: thread_idle_reap"},
        {"code": -32600, "message": "thread not found: thread_other"},
    ],
    ids=["other-refusal", "other-code", "other-thread"],
)
async def test_codex_other_settings_rejections_are_not_retried(
    client: httpx.AsyncClient, native_session: _NativeSession, error: dict[str, Any]
) -> None:
    session = native_session
    session.codex.response_errors["thread/settings/update"] = [error]
    response = await client.patch(
        f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
    )
    _assert_codex_refusal(session, response)
    assert session.codex.rpc_errors == [error]
    assert [method for method, _ in session.codex.requests if method.startswith("thread/")] == [
        "thread/settings/update"
    ]
    assert session.codex.launches == 1


@pytest.mark.parametrize("refused", ["resume", "retry"])
async def test_codex_unloaded_thread_recovery_refusal_does_not_loop(
    client: httpx.AsyncClient, native_session: _NativeSession, refused: str
) -> None:
    session = native_session
    session.codex.loaded_threads.clear()
    missing_thread = {"code": -32600, "message": f"thread not found: {session.codex.thread_id}"}
    error = {"code": -32600, "message": "saved thread is unavailable"}
    if refused == "resume":
        session.codex.response_errors["thread/resume"] = [error]
    else:
        session.codex.response_errors["thread/settings/update"] = [missing_thread, error]
    response = await client.patch(
        f"/v1/sessions/{session.session_id}",
        json={"model_override": _REQUESTED_MODEL, "reasoning_effort": "low"},
    )
    _assert_codex_refusal(session, response, combined=True)
    assert session.codex.rpc_errors == [missing_thread, error]
    expected = ["thread/settings/update", "thread/resume"]
    if refused == "retry":
        expected.append("thread/settings/update")
    assert [
        method for method, _ in session.codex.requests if method.startswith("thread/")
    ] == expected
    assert session.codex.launches == 1
    assert session.codex.saved_threads[session.codex.thread_id] == {
        "model": _PREVIOUS_MODEL,
        "effort": "high",
    }


@pytest.mark.parametrize("native_session", ["starting"], indirect=True)
async def test_codex_readiness_wait_rejects_another_sessions_bridge(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    missing = _observe_missing_bridge(monkeypatch, session)
    change = asyncio.create_task(
        client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
        )
    )
    try:
        await asyncio.wait_for(missing.wait(), timeout=budget(2))
        bridge.write_bridge_state(
            session.codex.bridge_dir,
            bridge.CodexNativeBridgeState(
                session_id="another-session",
                thread_id="another-thread",
                socket_path=session.codex.transport,
                codex_home=str(bridge.codex_home_for_bridge_dir(session.codex.bridge_dir)),
            ),
        )
        response = await asyncio.wait_for(change, timeout=budget(2))
    finally:
        change.cancel()
        await asyncio.gather(change, return_exceptions=True)
    _assert_codex_refusal(session, response)
    assert session.codex.requests == []
    assert session.codex.launches == 1


@pytest.mark.parametrize("combined", [False, True], ids=["model", "model-and-effort"])
async def test_model_change_recovers_unloaded_codex_thread(
    client: httpx.AsyncClient,
    native_session: _NativeSession,
    combined: bool,
) -> None:
    session = native_session
    session.codex.loaded_threads.clear()
    assert session.codex.server is not None
    assert bridge.read_bridge_state(session.codex.bridge_dir) is not None
    selection = {"model_override": _REQUESTED_MODEL}
    if combined:
        selection["reasoning_effort"] = "low"
    response = await client.patch(f"/v1/sessions/{session.session_id}", json=selection)

    session.assert_applied(response, effort="low" if combined else "high")
    assert session.codex.launches == 1
    assert session.codex.rpc_errors == [
        {"code": -32600, "message": f"thread not found: {session.codex.thread_id}"}
    ]
    assert [request for request in session.codex.requests if request[0] == "thread/resume"] == [
        ("thread/resume", {"threadId": session.codex.thread_id, "excludeTurns": True})
    ]


async def test_resuming_codex_thread_allows_combined_model_and_effort_change(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    session = native_session
    session.codex.loaded_threads.clear()
    rpc = app_server.client_for_transport(session.codex.transport)
    try:
        await rpc.connect()
        await rpc.request("thread/resume", {"threadId": session.codex.thread_id})
    finally:
        await rpc.close()
    response = await client.patch(
        f"/v1/sessions/{session.session_id}",
        json={"model_override": _REQUESTED_MODEL, "reasoning_effort": "low"},
    )
    session.assert_applied(response, effort="low")
    assert session.codex.launches == 1
    assert session.codex.rpc_errors == []


async def test_rapid_model_picks_after_idle_reap_recover_once(
    client: httpx.AsyncClient, native_session: _NativeSession
) -> None:
    session = native_session
    await session.reap(clear_bridge=False)
    models = [_REQUESTED_MODEL, _LATEST_MODEL] * 3
    responses = []
    for model in models:
        response = await client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": model}
        )
        responses.append(response)

    assert [response.status_code for response in responses] == [200] * len(models)
    assert session.codex.launches == 2
    assert [update["model"] for update in session.codex.applied_updates] == models
    session.assert_settings(_LATEST_MODEL)
    assert not any(event["type"] == "response.error" for event in session.events)


async def test_overlapping_model_picks_during_recovery_keep_the_latest_selection(
    client: httpx.AsyncClient, native_session: _NativeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = native_session
    await session.reap(clear_bridge=True)
    session.codex.ready_gate = asyncio.Event()
    missing = _observe_missing_bridge(monkeypatch, session)
    release_reply = asyncio.Event()
    latest_waiting = asyncio.Event()
    held_first_reply = False
    live_change = routes_core._live_settings_change(session.session_id)
    acquire = live_change.lock.acquire

    async def observe_acquire() -> bool:
        if live_change.lock.locked():
            latest_waiting.set()
        return await acquire()

    monkeypatch.setattr(live_change.lock, "acquire", observe_acquire)

    async def hold_first_reply(response: httpx.Response) -> None:
        nonlocal held_first_reply
        if response.request.url.path.endswith("/events") and not held_first_reply:
            held_first_reply = True
            await release_reply.wait()

    session.runner.event_hooks["response"].append(hold_first_reply)
    first = asyncio.create_task(
        client.patch(
            f"/v1/sessions/{session.session_id}", json={"model_override": _REQUESTED_MODEL}
        )
    )
    tasks = [first]
    try:
        await asyncio.wait_for(missing.wait(), timeout=budget(5))
        tasks.append(
            asyncio.create_task(
                client.patch(
                    f"/v1/sessions/{session.session_id}", json={"model_override": _LATEST_MODEL}
                )
            )
        )
        await asyncio.wait_for(latest_waiting.wait(), timeout=budget(5))
        assert not any(task.done() for task in tasks)
        session.codex.ready_gate.set()
        await asyncio.wait_for(session.codex.ready.wait(), timeout=budget(5))
        release_reply.set()
        earlier, latest = await asyncio.wait_for(asyncio.gather(*tasks), timeout=budget(10))
    finally:
        session.codex.ready_gate.set()
        release_reply.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        session.runner.event_hooks["response"].remove(hold_first_reply)

    assert latest.status_code == 200, latest.text
    assert latest.json()["model_override"] == _LATEST_MODEL
    session.assert_settings(_LATEST_MODEL)
    assert session.codex.launches == 2
    updates = [update["model"] for update in session.codex.applied_updates]
    errors = [event["error"] for event in session.events if event["type"] == "response.error"]
    assert earlier.status_code == 200, earlier.text
    assert updates == [_REQUESTED_MODEL, _LATEST_MODEL]
    assert errors == []
