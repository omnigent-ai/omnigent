"""Reset refuses a stale native terminal without closing its successor."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from omnigent.entities.session_resources import (
    SessionResourceView,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import (
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    terminal_launch_fence,
)
from omnigent.spec.types import AgentSpec
from omnigent.terminals import registry as registry_mod
from omnigent.terminals.registry import TerminalLaunchSupersededError, TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient, RunningFlagTerminalInstance

_CONV = "7c41f0a2b9de4c8fa1e05b6d3c2f8a91"
_TERMINAL = "goose"


@pytest.mark.asyncio
async def test_reset_during_terminal_launch_refuses_the_stale_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal that only finishes starting after a reset must not be attached."""
    entered_adapter = asyncio.Event()
    release_adapter = asyncio.Event()

    async def _adapter(ctx: Any) -> SessionResourceView:
        entered_adapter.set()
        await release_adapter.wait()
        return SessionResourceView(
            id=terminal_resource_id(_TERMINAL, "main"),
            type="terminal",
            session_id=ctx.session_id,
            name=_TERMINAL,
        )

    real_resolve_hook = orchestration.resolve_hook

    def _resolve_hook(provider: Any, name: str) -> Any:
        if name == "auto_create_terminal":
            return _adapter
        return real_resolve_hook(provider, name)

    monkeypatch.setattr(orchestration, "resolve_hook", _resolve_hook)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="t")

    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_1"}})]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        ensure = asyncio.create_task(
            client.post(
                f"/v1/sessions/{_CONV}/resources/terminals",
                json={
                    "terminal": _TERMINAL,
                    "session_key": "main",
                    "ensure_native_terminal": True,
                },
            )
        )
        await asyncio.wait_for(entered_adapter.wait(), timeout=5)

        reset = await client.post(f"/v1/sessions/{_CONV}/reset-state")
        assert reset.status_code == 200

        release_adapter.set()
        response = await asyncio.wait_for(ensure, timeout=5)

    assert response.status_code == 409, (
        "the terminal that finished starting after the reset was attached anyway "
        f"(status {response.status_code})"
    )
    assert response.json()["error"]["code"] == "session_reset_during_launch"


class _FakeCodexAppServer:
    """Stand in for the per-session ``codex app-server`` subprocess."""

    def __init__(self) -> None:
        self.closed = False
        self.policy_notice_pending = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _SessionSnapshotServer(NullServerClient):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    async def get(self, url: str, **kwargs: Any) -> Any:
        if url == f"/v1/sessions/{self.session_id}":
            return httpx.Response(
                200,
                json={"agent_id": "codex-agent", "created_at": 1},
                request=httpx.Request("GET", url),
            )
        return await super().get(url, **kwargs)


class _RecordingTerminal(RunningFlagTerminalInstance):
    async def launch(self, cwd: Path | None = None) -> None:
        del cwd
        self.running = True

    async def close(self) -> None:
        self.running = False

    def start_idle_watcher_thread(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _LatchingTerminal(_RecordingTerminal):
    async def launch(self, cwd: Path | None = None) -> None:
        self.entered.set()
        await self.release.wait()
        await super().launch(cwd)


def _codex_runner_app(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    terminal_registry: TerminalRegistry,
    resource_registry: SessionResourceRegistry,
    adapter: Any,
) -> Any:
    real_resolve_hook = orchestration.resolve_hook

    def _resolve_hook(provider: Any, name: str) -> Any:
        return adapter if name == "auto_create_terminal" else real_resolve_hook(provider, name)

    monkeypatch.setattr(orchestration, "resolve_hook", _resolve_hook)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="codex")

    return create_runner_app(
        terminal_registry=terminal_registry,
        resource_registry=resource_registry,
        spec_resolver=_resolver,
        server_client=_SessionSnapshotServer(session_id),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("pause_at", ["after_start", "during_start"])
@pytest.mark.asyncio
async def test_reset_then_codex_successor_remains_attachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pause_at: str,
) -> None:
    session_id = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    terminal_registry = TerminalRegistry()
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)

    class _PausedServer(_FakeCodexAppServer):
        async def start(self) -> None:
            entered.set()
            await release.wait()

    servers = [
        _PausedServer() if pause_at == "during_start" else _FakeCodexAppServer(),
        _FakeCodexAppServer(),
    ]
    instances: list[_RecordingTerminal] = []

    def _create(name: str, session_key: str, *args: Any, **kwargs: Any) -> TerminalCreateResult:
        del args, kwargs
        instance = _RecordingTerminal(
            name=name,
            session_key=session_key,
            socket_path=tmp_path / f"pane-{len(instances)}.sock",
            private_dir=tmp_path / f"pane-{len(instances)}",
        )
        instances.append(instance)
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(registry_mod, "create_terminal_instance", _create)
    spec = TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )
    calls = 0

    async def _adapter(ctx: Any) -> SessionResourceView:
        nonlocal calls
        index = calls
        calls += 1
        await orchestration._start_codex_app_server_for_launch(
            session_id, servers[index], ctx.registration_is_current
        )
        if index == 0 and pause_at == "after_start":
            entered.set()
            await release.wait()
        view = await ctx.resource_registry.launch_auxiliary_terminal(
            session_id=session_id,
            terminal_name="codex",
            session_key="main",
            spec=spec,
            resource_role=CODEX_NATIVE_TERMINAL_ROLE,
        )
        ctx.publish_event(
            session_id,
            {"type": "session.resource.created", "resource": session_resource_view_to_dict(view)},
        )
        return view

    app = _codex_runner_app(
        monkeypatch, session_id, terminal_registry, resource_registry, _adapter
    )
    path = f"/v1/sessions/{session_id}/resources/terminals"
    payload = {"terminal": "codex", "session_key": "main", "ensure_native_terminal": True}

    async with _runner_client(app) as client:
        old_request = asyncio.create_task(client.post(path, json=payload))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            reset = await client.post(f"/v1/sessions/{session_id}/reset-state")
            assert reset.status_code == 200
            assert servers[0].closed is (pause_at == "after_start")

            successor = await client.post(path, json=payload)
            assert successor.status_code == 200, successor.text
            assert len(instances) == 1
            successor_instance = instances[0]

            release.set()
            stale = await asyncio.wait_for(old_request, timeout=5)
            assert stale.status_code == 409
            assert servers[0].closed
            assert terminal_registry.get(session_id, "codex", "main") is successor_instance
            assert successor_instance.running
            assert orchestration._AUTO_CODEX_APP_SERVERS[session_id] is servers[1]
            assert not servers[1].closed

            attached = await client.get(f"{path}/{terminal_resource_id('codex', 'main')}")
            assert attached.status_code == 200, attached.text
            assert attached.json()["metadata"]["tmux_socket"] == str(
                successor_instance.socket_path
            )
        finally:
            release.set()
            if not old_request.done():
                await asyncio.gather(old_request, return_exceptions=True)

    queue = app.state.session_event_queues.get(session_id)
    events = [queue.get_nowait() for _ in range(queue.qsize())] if queue is not None else []
    created = [event for event in events if event.get("type") == "session.resource.created"]
    deleted = [event for event in events if event.get("type") == "session.resource.deleted"]
    assert len(created) == 1
    assert created[0]["resource"]["metadata"]["tmux_socket"] == str(successor_instance.socket_path)
    assert deleted == []

    attach_paths: list[str] = []

    async def _attach(websocket: Any, *, socket_path: str, **kwargs: Any) -> None:
        del kwargs
        attach_paths.append(socket_path)
        await websocket.send_text("attached")
        await websocket.close()

    monkeypatch.setattr("omnigent.runner.app.bridge_tmux_control_to_websocket", _attach)
    with TestClient(app).websocket_connect(
        f"{path}/{terminal_resource_id('codex', 'main')}/attach"
    ) as websocket:
        assert websocket.receive_text() == "attached"
    assert attach_paths == [str(successor_instance.socket_path)]
    await orchestration.teardown_codex_native_app_server(session_id)


@pytest.mark.parametrize("pause_at", ["server_start", "before_registry", "in_registry"])
@pytest.mark.asyncio
async def test_reset_without_successor_reclaims_codex_launch(
    pause_at: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    terminal_registry = TerminalRegistry()
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instances: list[_RecordingTerminal] = []

    class _AppServer(_FakeCodexAppServer):
        async def start(self) -> None:
            if pause_at == "server_start":
                entered.set()
                await release.wait()

    app_server = _AppServer()

    def _create(name: str, session_key: str, *args: Any, **kwargs: Any) -> TerminalCreateResult:
        del args, kwargs
        terminal_type = _LatchingTerminal if pause_at == "in_registry" else _RecordingTerminal
        instance = terminal_type(
            name=name,
            session_key=session_key,
            socket_path=tmp_path / "old.sock",
            private_dir=tmp_path / "old",
        )
        if isinstance(instance, _LatchingTerminal):
            instance.entered = entered
            instance.release = release
        instances.append(instance)
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(registry_mod, "create_terminal_instance", _create)
    spec = TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )

    async def _adapter(ctx: Any) -> SessionResourceView:
        await orchestration._start_codex_app_server_for_launch(
            session_id, app_server, ctx.registration_is_current
        )
        if pause_at == "before_registry":
            entered.set()
            await release.wait()
        view = await ctx.resource_registry.launch_auxiliary_terminal(
            session_id=session_id,
            terminal_name="codex",
            session_key="main",
            spec=spec,
            resource_role=CODEX_NATIVE_TERMINAL_ROLE,
        )
        ctx.publish_event(
            session_id,
            {"type": "session.resource.created", "resource": session_resource_view_to_dict(view)},
        )
        return view

    app = _codex_runner_app(
        monkeypatch, session_id, terminal_registry, resource_registry, _adapter
    )
    path = f"/v1/sessions/{session_id}/resources/terminals"
    payload = {"terminal": "codex", "session_key": "main", "ensure_native_terminal": True}

    async with _runner_client(app) as client:
        old_request = asyncio.create_task(client.post(path, json=payload))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            reset = await client.post(f"/v1/sessions/{session_id}/reset-state")
            assert reset.status_code == 200
            release.set()
            stale = await asyncio.wait_for(old_request, timeout=5)
        finally:
            release.set()
            if not old_request.done():
                await asyncio.gather(old_request, return_exceptions=True)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "session_reset_during_launch"
    assert terminal_registry.get(session_id, "codex", "main") is None
    assert all(not instance.running for instance in instances)
    assert app_server.closed
    assert session_id not in orchestration._AUTO_CODEX_APP_SERVERS
    queue = app.state.session_event_queues.get(session_id)
    events = [queue.get_nowait() for _ in range(queue.qsize())] if queue is not None else []
    assert not any(event.get("type") == "session.resource.created" for event in events)
    assert not any(event.get("type") == "session.resource.deleted" for event in events)


@pytest.mark.parametrize("with_successor", [False, True])
@pytest.mark.asyncio
async def test_reset_deletes_published_codex_pane_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_successor: bool,
) -> None:
    session_id = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    terminal_registry = TerminalRegistry()
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    app_servers = [_FakeCodexAppServer(), _FakeCodexAppServer()]
    instances: list[_RecordingTerminal] = []

    def _create(name: str, session_key: str, *args: Any, **kwargs: Any) -> TerminalCreateResult:
        del args, kwargs
        instance = _RecordingTerminal(
            name=name,
            session_key=session_key,
            socket_path=tmp_path / f"pane-{len(instances)}.sock",
            private_dir=tmp_path / f"pane-{len(instances)}",
        )
        instances.append(instance)
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(registry_mod, "create_terminal_instance", _create)
    spec = TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )

    launches = 0

    async def _adapter(ctx: Any) -> SessionResourceView:
        nonlocal launches
        index = launches
        launches += 1
        await orchestration._start_codex_app_server_for_launch(
            session_id, app_servers[index], ctx.registration_is_current
        )
        view = await ctx.resource_registry.launch_auxiliary_terminal(
            session_id=session_id,
            terminal_name="codex",
            session_key="main",
            spec=spec,
            resource_role=CODEX_NATIVE_TERMINAL_ROLE,
        )
        ctx.publish_event(
            session_id,
            {"type": "session.resource.created", "resource": session_resource_view_to_dict(view)},
        )
        if index == 0:
            entered.set()
            await release.wait()
        return view

    app = _codex_runner_app(
        monkeypatch, session_id, terminal_registry, resource_registry, _adapter
    )
    path = f"/v1/sessions/{session_id}/resources/terminals"
    payload = {"terminal": "codex", "session_key": "main", "ensure_native_terminal": True}

    async with _runner_client(app) as client:
        old_request = asyncio.create_task(client.post(path, json=payload))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            reset = await client.post(f"/v1/sessions/{session_id}/reset-state")
            assert reset.status_code == 200
            if with_successor:
                successor = await client.post(path, json=payload)
                assert successor.status_code == 200, successor.text
            release.set()
            stale = await asyncio.wait_for(old_request, timeout=5)
            if with_successor:
                attached = await client.get(f"{path}/{terminal_resource_id('codex', 'main')}")
                assert attached.status_code == 200, attached.text
                assert attached.json()["metadata"]["tmux_socket"] == str(instances[1].socket_path)
        finally:
            release.set()
            if not old_request.done():
                await asyncio.gather(old_request, return_exceptions=True)

    assert stale.status_code == 409
    assert not instances[0].running
    assert app_servers[0].closed
    if with_successor:
        assert len(instances) == 2
        assert terminal_registry.get(session_id, "codex", "main") is instances[1]
        assert instances[1].running
        assert orchestration._AUTO_CODEX_APP_SERVERS[session_id] is app_servers[1]
        assert not app_servers[1].closed
    else:
        assert len(instances) == 1
        assert terminal_registry.get(session_id, "codex", "main") is None
        assert session_id not in orchestration._AUTO_CODEX_APP_SERVERS
    queue = app.state.session_event_queues[session_id]
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    terminal_events = [
        event
        for event in events
        if event.get("type") in ("session.resource.created", "session.resource.deleted")
    ]
    expected_types = [
        "session.resource.created",
        "session.resource.deleted",
    ]
    if with_successor:
        expected_types.append("session.resource.created")
    assert [event["type"] for event in terminal_events] == expected_types
    assert terminal_events[1]["resource_id"] == terminal_resource_id("codex", "main")
    if with_successor:
        assert terminal_events[2]["resource"]["metadata"]["tmux_socket"] == str(
            instances[1].socket_path
        )
        await orchestration.teardown_codex_native_app_server(session_id)


@pytest.mark.asyncio
async def test_generic_terminal_reset_cannot_close_same_id_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    terminal_registry = TerminalRegistry()
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instances: list[_RecordingTerminal] = []

    def _create(name: str, session_key: str, *args: Any, **kwargs: Any) -> TerminalCreateResult:
        del args, kwargs
        instance = _RecordingTerminal(
            name=name,
            session_key=session_key,
            socket_path=tmp_path / f"pane-{len(instances)}.sock",
            private_dir=tmp_path / f"pane-{len(instances)}",
        )
        instances.append(instance)
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(registry_mod, "create_terminal_instance", _create)
    original_launch = resource_registry.launch_auxiliary_terminal
    launches = 0

    async def _launch(*args: Any, **kwargs: Any) -> SessionResourceView:
        nonlocal launches
        launches += 1
        if launches == 1:
            entered.set()
            await release.wait()
        return await original_launch(*args, **kwargs)

    monkeypatch.setattr(resource_registry, "launch_auxiliary_terminal", _launch)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="plain")

    app = create_runner_app(
        terminal_registry=terminal_registry,
        resource_registry=resource_registry,
        spec_resolver=_resolver,
        server_client=_SessionSnapshotServer(session_id),  # type: ignore[arg-type]
    )
    path = f"/v1/sessions/{session_id}/resources/terminals"
    payload = {"terminal": "bash", "session_key": "main", "spec": {"command": "bash"}}

    async with _runner_client(app) as client:
        old_request = asyncio.create_task(client.post(path, json=payload))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            reset = await client.post(f"/v1/sessions/{session_id}/reset-state")
            assert reset.status_code == 200
            successor = await client.post(path, json=payload)
            assert successor.status_code == 200, successor.text
            successor_instance = instances[0]
            release.set()
            stale = await asyncio.wait_for(old_request, timeout=5)
            assert stale.status_code == 409
            assert terminal_registry.get(session_id, "bash", "main") is successor_instance
            assert successor_instance.running
            attached = await client.get(f"{path}/{terminal_resource_id('bash', 'main')}")
            assert attached.status_code == 200
            assert attached.json()["metadata"]["tmux_socket"] == str(
                successor_instance.socket_path
            )
        finally:
            release.set()
            if not old_request.done():
                await asyncio.gather(old_request, return_exceptions=True)


@pytest.mark.asyncio
async def test_registry_existing_liveness_probe_cannot_adopt_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    registry = TerminalRegistry()
    instances: list[_RecordingTerminal] = []

    def _create(name: str, session_key: str, *args: Any, **kwargs: Any) -> TerminalCreateResult:
        del args, kwargs
        instance = _RecordingTerminal(
            name=name,
            session_key=session_key,
            socket_path=tmp_path / f"pane-{len(instances)}.sock",
            private_dir=tmp_path / f"pane-{len(instances)}",
        )
        instances.append(instance)
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(registry_mod, "create_terminal_instance", _create)
    spec = TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )
    old = await registry.launch(session_id, "codex", "main", spec)

    async def _old_is_alive() -> bool:
        entered.set()
        await release.wait()
        return old.running

    monkeypatch.setattr(old, "is_alive", _old_is_alive)
    old_request = asyncio.create_task(registry.launch(session_id, "codex", "main", spec))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        registry.supersede_inflight_launches(session_id)
        await registry.cleanup_conversation(session_id)
        successor = await registry.launch(session_id, "codex", "main", spec)
        release.set()
        with pytest.raises(TerminalLaunchSupersededError):
            await asyncio.wait_for(old_request, timeout=5)
        assert registry.get(session_id, "codex", "main") is successor
        assert successor.running
    finally:
        release.set()
        if not old_request.done():
            await asyncio.gather(old_request, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_launch_cannot_cancel_successor_forwarder() -> None:
    session_id = uuid.uuid4().hex
    successor = asyncio.create_task(asyncio.Event().wait())
    orchestration._AUTO_FORWARDER_TASKS[session_id] = successor
    generation = 0
    try:
        with terminal_launch_fence(lambda: generation == 0):
            generation = 1
            with pytest.raises(TerminalLaunchSupersededError):
                await orchestration._cancel_auto_forwarder_task(session_id)
        assert orchestration._AUTO_FORWARDER_TASKS[session_id] is successor
        assert not successor.cancelled()
    finally:
        orchestration._AUTO_FORWARDER_TASKS.pop(session_id, None)
        successor.cancel()
        await asyncio.gather(successor, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_forwarder_cleanup_cancels_only_its_task() -> None:
    session_id = uuid.uuid4().hex
    old = asyncio.create_task(asyncio.Event().wait())
    successor = asyncio.create_task(asyncio.Event().wait())
    orchestration._AUTO_FORWARDER_TASKS[session_id] = successor
    try:
        await orchestration._cancel_auto_forwarder_task(session_id, expected=old)
        assert old.cancelled()
        assert orchestration._AUTO_FORWARDER_TASKS[session_id] is successor
        assert not successor.cancelled()
    finally:
        orchestration._AUTO_FORWARDER_TASKS.pop(session_id, None)
        successor.cancel()
        await asyncio.gather(old, successor, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_opencode_forwarder_preserves_successor_server() -> None:
    session_id = uuid.uuid4().hex

    class _Server:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    old_server = _Server()
    successor = _Server()

    class _Forwarder:
        async def run(self) -> None:
            orchestration._AUTO_OPENCODE_SERVERS[session_id] = successor  # type: ignore[assignment]

    orchestration._AUTO_OPENCODE_SERVERS[session_id] = old_server  # type: ignore[assignment]
    try:
        await orchestration._supervise_opencode_forwarder(
            session_id,
            old_server,
            _Forwarder(),  # type: ignore[arg-type]
        )
        assert orchestration._AUTO_OPENCODE_SERVERS[session_id] is successor
        assert old_server.closed
        assert not successor.closed
    finally:
        orchestration._AUTO_OPENCODE_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_stale_opencode_launch_cannot_clear_successor_bridge_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.harnesses.opencode_native import bridge as opencode_bridge

    session_id = uuid.uuid4().hex
    entered = asyncio.Event()
    release = asyncio.Event()
    generation = 0
    cleared: list[Path] = []

    class _Server:
        def __init__(self, *, pause_on_close: bool = False) -> None:
            self.closed = False
            self.pause_on_close = pause_on_close

        async def close(self) -> None:
            if self.pause_on_close:
                entered.set()
                await release.wait()
            self.closed = True

    old_server = _Server(pause_on_close=True)
    successor = _Server()

    async def _launch_config(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(workspace=tmp_path)

    monkeypatch.setattr(orchestration, "_opencode_native_launch_config", _launch_config)
    monkeypatch.setattr(opencode_bridge, "prepare_bridge_dir", lambda _sid: tmp_path)
    monkeypatch.setattr(opencode_bridge, "write_relay_bridge_config", lambda _dir: None)
    monkeypatch.setattr(opencode_bridge, "clear_bridge_state", cleared.append)
    orchestration._AUTO_OPENCODE_SERVERS[session_id] = old_server  # type: ignore[assignment]

    async def _launch() -> None:
        with terminal_launch_fence(lambda: generation == 0):
            await orchestration._auto_create_opencode_terminal(
                session_id,
                SessionResourceRegistry(),
                lambda _sid, _event: None,
            )

    task = asyncio.create_task(_launch())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        generation = 1
        orchestration._AUTO_OPENCODE_SERVERS[session_id] = successor  # type: ignore[assignment]
        release.set()
        with pytest.raises(TerminalLaunchSupersededError):
            await asyncio.wait_for(task, timeout=5)
        assert old_server.closed
        assert orchestration._AUTO_OPENCODE_SERVERS[session_id] is successor
        assert not successor.closed
        assert cleared == []
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        orchestration._AUTO_OPENCODE_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_stale_discard_preserves_successor_with_same_resource_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_registry = TerminalRegistry()
    successor = _RecordingTerminal(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "successor.sock",
        private_dir=tmp_path / "successor",
        running=True,
    )
    terminal_registry._by_conversation[_CONV] = {("codex", "main"): successor}
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    successor_server = _FakeCodexAppServer()
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, _CONV, successor_server)
    events: list[tuple[str, dict[str, Any]]] = []
    ctx = orchestration.NativeLaunchContext(
        session_id=_CONV,
        resource_registry=resource_registry,
        publish_event=lambda sid, event: events.append((sid, event)),
    )

    await orchestration._discard_terminal_reset_mid_launch(
        ctx,
        terminal_name="codex",
        view=SessionResourceView(
            id=terminal_resource_id("codex", "main"),
            type="terminal",
            session_id=_CONV,
            name="codex:main",
            metadata={
                "terminal_name": "codex",
                "session_key": "main",
                "tmux_socket": str(tmp_path / "stale.sock"),
                "tmux_target": "main",
            },
        ),
    )

    assert terminal_registry.get(_CONV, "codex", "main") is successor
    assert successor.running
    assert orchestration._AUTO_CODEX_APP_SERVERS[_CONV] is successor_server
    assert not successor_server.closed
    assert events == []


@pytest.mark.asyncio
async def test_discard_closes_its_registered_pane_and_publishes_delete(
    tmp_path: Path,
) -> None:
    """A matching pane is closed and its published resource is deleted."""
    events: list[tuple[str, dict[str, Any]]] = []
    terminal_registry = TerminalRegistry()
    stale = _RecordingTerminal(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "stale.sock",
        private_dir=tmp_path / "stale",
        running=True,
    )
    terminal_registry._by_conversation[_CONV] = {("codex", "main"): stale}
    resource_registry = SessionResourceRegistry(terminal_registry=terminal_registry)

    stale_id = terminal_resource_id("codex", "main")
    ctx = orchestration.NativeLaunchContext(
        session_id=_CONV,
        resource_registry=resource_registry,
        publish_event=lambda session_id, event: events.append((session_id, event)),
    )
    await orchestration._discard_terminal_reset_mid_launch(
        ctx,
        terminal_name="codex",
        view=SessionResourceView(
            id=stale_id,
            type="terminal",
            session_id=_CONV,
            name="codex",
            metadata={
                "terminal_name": "codex",
                "session_key": "main",
                "tmux_socket": str(stale.socket_path),
                "tmux_target": stale.tmux_target,
            },
        ),
    )

    assert terminal_registry.get(_CONV, "codex", "main") is None
    assert not stale.running
    assert [event for _session, event in events] == [
        {
            "type": "session.resource.deleted",
            "resource_id": stale_id,
            "resource_type": "terminal",
            "session_id": _CONV,
        }
    ]
