"""A top-level codex-native pane must not be held "busy" by a stale status.

For codex-native the turn start publishes ``running``, the runner drops its own
turn-end ``idle`` (the forwarder owns idle), and the forwarder's ``idle`` reaches
the runner only as a relayed ``external_session_status``. Interrupt and
stop_session publish no ``session.status`` for codex. The runner's status book
must end the ``running`` episode on the relayed idle, and on an accepted
``turn/interrupt`` even when the forwarder's idle is lost.

Everything below drives production code: the runner is built with
``create_runner_app`` and every state change goes through its real HTTP routes
(``POST /v1/sessions``, ``POST /v1/sessions/{id}/events`` with ``message``,
``external_session_status``, ``interrupt``, ``stop_session``, and
``DELETE /v1/sessions/{id}``). The busy check and the reap decision are the
runner's own closures reached through ``app.state.native_pane_reaper``. The
status is only ever READ (``app.state.native_pane_status``).

Fakes (all outside the logic under test):

* tmux probes ``_list_tmux_clients`` / ``_tmux_window_activity_at`` -> "no
  clients, last output 3 hours ago".
* the codex pane is a ``TerminalInstance`` with ``is_alive``/``close``/
  ``start_idle_watcher_thread`` stubbed (no tmux server), registered through the
  real ``SessionResourceRegistry.observe_auxiliary_terminal`` with the real
  ``CODEX_NATIVE_TERMINAL_ROLE``.
* the harness subprocess is the suite's ``_FakeProcessManager`` +
  ``_ScriptedHarnessClient`` streaming ``response.created`` /
  ``response.completed``.
* the Codex app-server JSON-RPC client (``client_for_transport``) records every
  request and returns an empty success.
* ``NullServerClient`` for runner -> server calls.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.inner.terminal import TerminalInstance
from omnigent.native import native_cost_popup
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import (
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals.pane_reaper import PaneRef
from omnigent.terminals.registry import TerminalRegistry

# Re-exported so its autouse applies here too: keeps any launch-catalog consult
# off the real harness CLIs and the developer's ~/.omnigent store.
from tests.runner.conftest import (  # noqa: F401
    _FakeProcessManager,
    _isolated_model_catalog_store,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_AGENT_ID = "880b5afda28ad55ff74cbeb9b5fc67fb"
_CODEX_WS = "ws://127.0.0.1:43299"
_IDLE_TIMEOUT_S = 0.05
_PANE_SILENT_FOR_S = 3 * 3600.0


class _SetupError(RuntimeError):
    """A precondition of the scenario failed (NOT the behavior under test)."""


def _require(cond: object, msg: str) -> None:
    if not cond:
        raise _SetupError(msg)


class _RecordingCodexAppServerClient:
    """Fake Codex app-server JSON-RPC client: records requests, returns success."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.connected = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connected += 1

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        return {"result": {}}

    async def close(self) -> None:
        self.closed += 1


@dataclass
class _Rig:
    app: FastAPI
    conv_id: str
    terminal_registry: TerminalRegistry
    resources: SessionResourceRegistry
    pm: _FakeProcessManager
    harness: _ScriptedHarnessClient
    codex_client: _RecordingCodexAppServerClient
    pane_instance: TerminalInstance
    tmux_probe_calls: list[str] = field(default_factory=list)
    pane_closed: list[bool] = field(default_factory=list)
    watcher_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def mirror(self) -> Mapping[str, str]:
        """Read-only view of the runner's session status book."""
        return self.app.state.native_pane_status

    def panes(self) -> list[PaneRef]:
        """Panes the production reaper would consider (``_native_panes_for_reaper``)."""
        return [
            p
            for p in self.app.state.native_pane_reaper._list_native_panes()
            if p.conversation_id == self.conv_id
        ]

    def pane(self) -> PaneRef:
        panes = self.panes()
        _require(len(panes) == 1, f"expected exactly one codex pane listed, got {panes!r}")
        return panes[0]

    async def is_busy(self) -> bool:
        """The runner's real ``_native_pane_is_busy`` via the reaper."""
        return await self.app.state.native_pane_reaper._is_busy(self.pane())

    async def two_reaper_scans(self) -> None:
        """Two real ``_scan_once`` passes > idle timeout apart (first arms the clock)."""
        reaper = self.app.state.native_pane_reaper
        await reaper._scan_once()
        await asyncio.sleep(_IDLE_TIMEOUT_S * 2)
        await reaper._scan_once()

    def pane_registered(self) -> bool:
        return self.terminal_registry.get(self.conv_id, "codex", "main") is not None


def _plant_codex_pane(
    registry: TerminalRegistry,
    conv_id: str,
    tmp_path: Path,
    closed: list[bool],
    watcher_calls: list[dict[str, Any]],
) -> TerminalInstance:
    """Put a live-looking codex/main pane in the terminal registry (no tmux)."""
    private_dir = tmp_path / f"pane-{conv_id[:8]}"
    private_dir.mkdir()
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=private_dir / "tmux.sock",
        private_dir=private_dir,
    )
    instance.running = True

    async def _alive() -> bool:
        return instance.running

    async def _close() -> None:
        instance.running = False
        closed.append(True)

    def _watch(on_idle: Callable[[], None] | None = None, **kwargs: Any) -> None:
        watcher_calls.append({"on_idle": on_idle, **kwargs})

    instance.is_alive = _alive  # type: ignore[method-assign]
    instance.close = _close  # type: ignore[method-assign]
    instance.start_idle_watcher_thread = _watch  # type: ignore[method-assign]
    with registry._lock:
        registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance
        registry._instance_locks[(conv_id, "codex", "main")] = threading.Lock()
    return instance


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Rig:
    """A runner with one top-level codex-native session and its codex pane."""
    tmux_probe_calls: list[str] = []

    def _no_clients(*_args: Any) -> list[str]:
        tmux_probe_calls.append("list_clients")
        return []

    def _no_activity(*_args: Any) -> float | None:
        # tmux's window_activity for a pane that last printed 3 hours ago.
        tmux_probe_calls.append("window_activity")
        return time.time() - _PANE_SILENT_FOR_S

    # Bound by name when the app is built, so stub before building.
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", _no_clients)
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", _no_activity)
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S", str(_IDLE_TIMEOUT_S))

    codex_client = _RecordingCodexAppServerClient()

    def _client_for_transport(
        transport: str, *, client_name: str = "omnigent"
    ) -> _RecordingCodexAppServerClient:
        _require(transport == _CODEX_WS, f"unexpected app-server transport {transport!r}")
        del client_name
        return codex_client

    monkeypatch.setattr(codex_native_app_server, "client_for_transport", _client_for_transport)

    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_codex_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_codex_1"}}),
        ]
    )
    pm = _FakeProcessManager(harness)
    terminal_registry = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=terminal_registry)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=resources,
    )
    _require(app.state.native_pane_reaper is not None, "reaper not wired")

    conv_id = uuid.uuid4().hex
    closed: list[bool] = []
    watcher_calls: list[dict[str, Any]] = []
    instance = _plant_codex_pane(terminal_registry, conv_id, tmp_path, closed, watcher_calls)
    # Real registry method, real codex role, same auxiliary lifecycle as the
    # codex launch path (``launch_auxiliary_terminal``).
    await resources.observe_auxiliary_terminal(
        conv_id, "codex", "main", instance, resource_role=CODEX_NATIVE_TERMINAL_ROLE
    )

    # Codex bridge state as the runner-spawned app-server leaves it mid-turn.
    codex_native_bridge.write_bridge_state(
        codex_native_bridge.bridge_dir_for_bridge_id(conv_id),
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path=_CODEX_WS,
            thread_id="thread_codex_top",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_codex_top",
        ),
    )

    return _Rig(
        app=app,
        conv_id=conv_id,
        terminal_registry=terminal_registry,
        resources=resources,
        pm=pm,
        harness=harness,
        codex_client=codex_client,
        pane_instance=instance,
        tmux_probe_calls=tmux_probe_calls,
        pane_closed=closed,
        watcher_calls=watcher_calls,
    )


async def _create_session_and_run_turn(rig: _Rig, client: Any) -> None:
    """POST /v1/sessions, then a real user ``message`` turn, and let it finish."""
    resp = await client.post(
        "/v1/sessions", json={"session_id": rig.conv_id, "agent_id": _AGENT_ID}
    )
    _require(resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}")
    _require(rig.pane_registered(), "planted codex pane vanished after session create")
    _require(rig.panes(), "reaper does not list the codex pane (role not codex-native?)")

    # Baseline control: before any turn the same busy check reads idle and
    # reaches the tmux probes -- so the fakes themselves say "idle".
    rig.tmux_probe_calls.clear()
    _require(not await rig.is_busy(), "pane already busy before any turn")
    _require(
        rig.tmux_probe_calls == ["list_clients", "window_activity"],
        f"baseline busy check did not reach tmux probes: {rig.tmux_probe_calls!r}",
    )

    resp = await client.post(
        f"/v1/sessions/{rig.conv_id}/events",
        json={
            "type": "message",
            "agent_id": _AGENT_ID,
            "content": [{"type": "input_text", "text": "do the thing"}],
        },
    )
    _require(resp.status_code == 202, f"turn start failed: {resp.status_code} {resp.text}")
    _require(rig.mirror.get(rig.conv_id) == "running", "turn start did not publish running")
    turn = rig.app.state.active_turns.get(rig.conv_id)
    if isinstance(turn, asyncio.Task):
        await asyncio.wait_for(turn, timeout=10)
    for _ in range(200):
        if rig.conv_id not in rig.app.state.active_turns:
            break
        await asyncio.sleep(0.01)
    _require(rig.harness.posted_bodies, "harness never received the turn")
    _require(rig.conv_id not in rig.app.state.active_turns, "runner turn never finished")
    _require(not rig.pm.has_active_turn(rig.conv_id), "process manager still has a live turn")
    _require(
        rig.resources.session_turn_is_active(rig.conv_id),
        "registry did not record the native turn as started",
    )


async def _relay_forwarder_idle(rig: _Rig, client: Any) -> None:
    """The forwarder's turn-end idle, as the server relays it to the runner."""
    resp = await client.post(
        f"/v1/sessions/{rig.conv_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "idle", "output": "all done"},
        },
    )
    _require(resp.status_code == 204, f"relayed idle rejected: {resp.status_code} {resp.text}")
    # The runner DID receive and process the idle: the resource registry now
    # knows the native turn is over.
    _require(
        not rig.resources.session_turn_is_active(rig.conv_id),
        "relayed idle did not reach resource_registry.note_external_session_status",
    )


async def _interrupt(rig: _Rig, client: Any) -> None:
    before = len(rig.codex_client.requests)
    resp = await client.post(f"/v1/sessions/{rig.conv_id}/events", json={"type": "interrupt"})
    _require(resp.status_code == 204, f"interrupt failed: {resp.status_code} {resp.text}")
    _require(
        rig.codex_client.requests[before:]
        == [("turn/interrupt", {"threadId": "thread_codex_top", "turnId": "turn_codex_top"})],
        f"interrupt did not reach Codex turn/interrupt: {rig.codex_client.requests!r}",
    )


async def _stop(rig: _Rig, client: Any) -> None:
    before = len(rig.codex_client.requests)
    resp = await client.post(f"/v1/sessions/{rig.conv_id}/events", json={"type": "stop_session"})
    _require(resp.status_code == 204, f"stop_session failed: {resp.status_code} {resp.text}")
    _require(
        rig.codex_client.requests[before:]
        == [("turn/interrupt", {"threadId": "thread_codex_top", "turnId": "turn_codex_top"})],
        f"stop_session did not reach Codex turn/interrupt: {rig.codex_client.requests!r}",
    )


# ---------------------------------------------------------------------------
# Scenario 1: real turn -> relayed forwarder idle -> reaper busy check + scans
# ---------------------------------------------------------------------------


async def test_codex_toplevel_relayed_idle_expected(rig: _Rig) -> None:
    async with _runner_client(rig.app) as client:
        await _create_session_and_run_turn(rig, client)
        await _relay_forwarder_idle(rig, client)

        assert await rig.is_busy() is False, (
            "forwarder reported idle, no runner turn, no tmux client, silent pane: "
            "the pane must read idle to the reaper"
        )
        await rig.two_reaper_scans()
        assert not rig.pane_registered(), "idle codex pane must be reaped"


# ---------------------------------------------------------------------------
# Scenario 2: ...then a real interrupt and a real stop_session (codex paths in
# runner/native/interrupt.py). Neither publishes session.status.
# ---------------------------------------------------------------------------


async def test_codex_toplevel_interrupt_and_stop_after_idle_expected(rig: _Rig) -> None:
    async with _runner_client(rig.app) as client:
        await _create_session_and_run_turn(rig, client)
        await _relay_forwarder_idle(rig, client)

        await _interrupt(rig, client)
        assert await rig.is_busy() is False, "after idle + interrupt the pane must read idle"
        await _stop(rig, client)
        assert await rig.is_busy() is False, "after idle + stop_session the pane must read idle"


# ---------------------------------------------------------------------------
# Scenario 3: forwarder idle LOST (forwarder dead/stalled), user interrupts and
# stops. Codex accepting turn/interrupt ends the turn.
# ---------------------------------------------------------------------------


async def test_codex_toplevel_stop_without_forwarder_idle_expected(rig: _Rig) -> None:
    async with _runner_client(rig.app) as client:
        await _create_session_and_run_turn(rig, client)

        await _interrupt(rig, client)
        await _stop(rig, client)
        # Codex accepted turn/interrupt, no runner turn, no client, silent pane.
        assert await rig.is_busy() is False, (
            "after a successful stop_session the pane must not stay latched busy"
        )


# ---------------------------------------------------------------------------
# Session DELETE forgets the session's status.
# ---------------------------------------------------------------------------


async def test_codex_toplevel_session_delete_clears_status(rig: _Rig) -> None:
    async with _runner_client(rig.app) as client:
        await _create_session_and_run_turn(rig, client)
        pane = rig.pane()

        resp = await client.delete(f"/v1/sessions/{rig.conv_id}")
        assert resp.status_code < 300, resp.text
        assert rig.conv_id not in rig.mirror
        assert await rig.app.state.native_pane_reaper._is_busy(pane) is False
        assert not rig.pane_registered()
        assert rig.panes() == []
