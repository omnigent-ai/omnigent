"""``POST /v1/sessions/{id}/reset-state`` must fence a session-init terminal creator.

The native terminal start is held at a latch across the reset; afterwards nothing from
the pre-reset agent spec (terminal, created event, codex app-server, forwarder) may remain.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import session_resource_view_to_dict
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import BRIDGE_ID_LABEL_KEY
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.codex_native.bridge import CODEX_NATIVE_BRIDGE_ID_LABEL_KEY
from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from omnigent.terminals import registry as terminal_registry_mod
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient, RunningFlagTerminalInstance

_SESSION_ID = "7f3c0a2e9b4d4e1f8a6c5d2b1e0f9a8c"
_AGENT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


@dataclass
class _StartLatch:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    closed: int = 0


class _LatchedTerminalInstance(RunningFlagTerminalInstance):
    """Terminal whose start blocks at the latch; the registry registers it only after release."""

    latch: _StartLatch

    async def launch(self, *, cwd: Path | None = None) -> None:
        del cwd
        self.running = True
        self.latch.started.set()
        await self.latch.release.wait()

    async def close(self) -> None:
        self.running = False
        self.latch.closed += 1


def _latch_terminal_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, latch: _StartLatch
) -> None:
    def _create(name: str, session_key: str, spec: Any, **kwargs: Any) -> TerminalCreateResult:
        del spec, kwargs
        instance = _LatchedTerminalInstance(
            name=name,
            session_key=session_key,
            socket_path=tmp_path / f"{name}-{session_key}.sock",
            private_dir=tmp_path / f"{name}-{session_key}",
            running=False,
        )
        instance.latch = latch
        return TerminalCreateResult(instance=instance, cwd=tmp_path)

    monkeypatch.setattr(terminal_registry_mod, "create_terminal_instance", _create)
    # The pane watcher probes tmux; with no tmux server behind the latched terminal it
    # would report an exit and unregister it, hiding whether the reset fenced it.
    monkeypatch.setattr(
        SessionResourceRegistry, "_start_terminal_activity_watcher", lambda *a, **k: None
    )


class _SessionServerClient(NullServerClient):
    """Server stub whose session reads name the session's own bridge and an empty history."""

    def __init__(self, labels: dict[str, str]) -> None:
        self._labels = labels

    class _JsonResponse(NullServerClient._Response):
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        path = url.split("?", 1)[0]
        if path.endswith(("/items", "/child_sessions")):
            return self._JsonResponse({"data": [], "has_more": False})
        if path.endswith("/labels"):
            return self._JsonResponse({"labels": self._labels})
        if path == f"/v1/sessions/{_SESSION_ID}":
            return self._JsonResponse(
                {
                    "id": _SESSION_ID,
                    "agent_id": _AGENT_ID,
                    "created_at": 10,
                    "labels": self._labels,
                }
            )
        return await super().get(url, **kwargs)


class _FakeCodexAppServer:
    def __init__(self) -> None:
        self.listen_url = "ws://127.0.0.1:1"
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _terminal_spec(tmp_path: Path, command: str) -> TerminalEnvSpec:
    return TerminalEnvSpec(
        command=command,
        os_env=OSEnvSpec(type="caller_process", cwd=str(tmp_path)),
    )


def _build_app(
    tmp_path: Path, harness: str, labels: dict[str, str]
) -> tuple[Any, TerminalRegistry, SessionResourceRegistry]:
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(
        terminal_registry=terminal_registry,
        runner_workspace=tmp_path,
        per_session_workspace=False,
    )
    spec = AgentSpec(
        spec_version=1,
        name="agent-a",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionServerClient(labels),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=registry,
        runner_workspace=tmp_path,
        per_session_workspace=False,
    )
    return app, terminal_registry, registry


async def _reset_while_terminal_is_starting(
    client: httpx.AsyncClient, latch: _StartLatch
) -> httpx.Response:
    """Run session init, reset the session once its terminal start is held, then let it finish."""
    create = asyncio.create_task(
        client.post("/v1/sessions", json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID})
    )
    try:
        await asyncio.wait_for(latch.started.wait(), timeout=5.0)
        reset = await client.post(f"/v1/sessions/{_SESSION_ID}/reset-state")
        assert reset.status_code == 200, reset.text
        assert reset.json()["reset"] is True
    finally:
        latch.release.set()
    created = await asyncio.wait_for(create, timeout=10.0)
    assert created.status_code == 201, created.text
    return created


async def _settle(done: Callable[[], bool], timeout: float = 1.0) -> None:
    """Allow deferred teardown after session init completes."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not done() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)


def _resource_events(events: list[dict[str, Any]], resource_id: str) -> tuple[int, int]:
    created = sum(
        1
        for e in events
        if e.get("type") == "session.resource.created"
        and (e.get("resource") or {}).get("id") == resource_id
    )
    deleted = sum(
        1
        for e in events
        if e.get("type") == "session.resource.deleted" and e.get("resource_id") == resource_id
    )
    return created, deleted


@pytest.mark.asyncio
async def test_reset_state_discards_terminal_that_finishes_starting_after_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "claude-bridge")
    latch = _StartLatch()
    _latch_terminal_start(monkeypatch, tmp_path, latch)

    async def _start_claude_terminal(
        session_id: str, resource_registry: SessionResourceRegistry, publish_event: Any, **_: Any
    ) -> Any:
        view = await resource_registry.launch_required_terminal(
            session_id=session_id,
            terminal_name="claude",
            session_key="main",
            spec=_terminal_spec(tmp_path, "claude"),
            resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
        )
        publish_event(
            session_id,
            {"type": "session.resource.created", "resource": session_resource_view_to_dict(view)},
        )
        return view

    monkeypatch.setattr(orchestration, "_auto_create_claude_terminal", _start_claude_terminal)
    app, terminal_registry, registry = _build_app(
        tmp_path, "claude-native", {BRIDGE_ID_LABEL_KEY: _SESSION_ID}
    )

    try:
        async with _runner_client(app) as client:
            await _reset_while_terminal_is_starting(client, latch)
            await _settle(lambda: terminal_registry.get(_SESSION_ID, "claude", "main") is None)

            listed = await client.get(f"/v1/sessions/{_SESSION_ID}/resources/terminals")
            assert listed.status_code == 200, listed.text
            events = _drain_session_event_queue(app.state.session_event_queues.get(_SESSION_ID))
            created, deleted = _resource_events(events, "terminal_claude_main")
            observed = {
                "terminal_registered_after_reset": terminal_registry.get(
                    _SESSION_ID, "claude", "main"
                )
                is not None,
                "terminals_listed_after_reset": [t["id"] for t in listed.json()["data"]],
                "created_events_without_deleted": max(created - deleted, 0),
            }
            assert observed == {
                "terminal_registered_after_reset": False,
                "terminals_listed_after_reset": [],
                "created_events_without_deleted": 0,
            }, "the previous agent's terminal survived reset-state"
    finally:
        latch.release.set()
        await registry.cleanup_session(_SESSION_ID)


@pytest.mark.asyncio
async def test_reset_state_closes_codex_app_server_stored_before_terminal_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    latch = _StartLatch()
    _latch_terminal_start(monkeypatch, tmp_path, latch)
    app_server = _FakeCodexAppServer()
    forwarder_gate = asyncio.Event()

    async def _start_codex_terminal(
        session_id: str, resource_registry: SessionResourceRegistry, publish_event: Any, **_: Any
    ) -> Any:
        # Same order as the codex creator: app-server stored, then the TUI terminal registers.
        orchestration._AUTO_CODEX_APP_SERVERS[session_id] = app_server  # type: ignore[assignment]
        view = await resource_registry.launch_auxiliary_terminal(
            session_id=session_id,
            terminal_name="codex",
            session_key="main",
            spec=_terminal_spec(tmp_path, "codex"),
            resource_role=CODEX_NATIVE_TERMINAL_ROLE,
        )
        publish_event(
            session_id,
            {"type": "session.resource.created", "resource": session_resource_view_to_dict(view)},
        )
        orchestration._register_auto_forwarder_task(
            session_id,
            asyncio.create_task(forwarder_gate.wait(), name=f"codex-forwarder-{session_id}"),
        )
        return view

    monkeypatch.setattr(orchestration, "_auto_create_codex_terminal", _start_codex_terminal)
    app, terminal_registry, registry = _build_app(
        tmp_path, "codex-native", {CODEX_NATIVE_BRIDGE_ID_LABEL_KEY: _SESSION_ID}
    )

    try:
        async with _runner_client(app) as client:
            await _reset_while_terminal_is_starting(client, latch)
            await _settle(
                lambda: (
                    app_server.closed
                    and _SESSION_ID not in orchestration._AUTO_CODEX_APP_SERVERS
                    and terminal_registry.get(_SESSION_ID, "codex", "main") is None
                )
            )

            observed = {
                "terminal_registered_after_reset": terminal_registry.get(
                    _SESSION_ID, "codex", "main"
                )
                is not None,
                "app_server_registered_after_reset": orchestration._AUTO_CODEX_APP_SERVERS.get(
                    _SESSION_ID
                )
                is app_server,
                "app_server_closed": app_server.closed,
                "forwarder_registered_after_reset": _SESSION_ID
                in orchestration._AUTO_FORWARDER_TASKS,
            }
            assert observed == {
                "terminal_registered_after_reset": False,
                "app_server_registered_after_reset": False,
                "app_server_closed": True,
                "forwarder_registered_after_reset": False,
            }, "the pre-reset codex creator's resources survived reset-state"
    finally:
        latch.release.set()
        forwarder_gate.set()
        await orchestration.teardown_codex_native_app_server(_SESSION_ID)
        await registry.cleanup_session(_SESSION_ID)
