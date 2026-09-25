"""A relayed ``idle`` that beats the pane's own ``idle`` must not strand the reaper.

For a status-emitting native terminal (claude-native with Claude's
``sessions/<pid>.json`` file, or any PTY-status role) the forwarder's relayed
turn-end edge (``external_session_status: idle``) moves the wire dedup baseline
to ``idle``, so the pane's own ``idle`` that follows is swallowed on the wire —
the server hears one idle, as it should. The runner's status book must still
record both edges, so the reaper sees a finished, silent pane as idle without
waiting for a tunnel-reconnect resync.

Every test builds the real runner with ``create_runner_app`` and drives the real
registry, the real ``SessionStatusPoller`` reading a real temp status file, the
real events route and the real reaper. Only tmux probes, the tmux watcher
thread (callbacks captured and invoked from a worker thread) and the Omnigent
server client are faked.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.native import native_cost_popup
from omnigent.runner.app import _session_event_queues_ref, create_runner_app
from omnigent.runner.resource_registry import (
    _STATUS_EMITTING_TERMINAL_ROLES,
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CURSOR_NATIVE_TERMINAL_ROLE,
    GOOSE_NATIVE_TERMINAL_ROLE,
    HERMES_NATIVE_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    QWEN_NATIVE_TERMINAL_ROLE,
)
from omnigent.terminals.pane_reaper import PaneRef
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

# Fake pane pid; names the status file ``<CLAUDE_CONFIG_DIR>/sessions/<pid>.json``
# the real poller resolves, exactly as ``#{pane_pid}`` does in production.
_PANE_PID = 4178604
_CLAUDE_SESSION_UUID = "0d5c8f3e-7a51-4c7b-9d62-3f1e2a9b8c10"


@dataclass
class _Pane:
    """One live runner app with one observed native agent terminal."""

    app: Any
    conv_id: str
    terminal_name: str
    terminal_registry: TerminalRegistry
    status_file: Path
    callbacks: dict[str, Callable[[], None]]
    output_clock: dict[str, float]
    closed_instances: list[str] = field(default_factory=list)
    _mtime: float = field(default_factory=lambda: time.time() - 1000.0)

    @property
    def registry(self) -> Any:
        return self.app.state.session_resource_registry

    @property
    def mirror(self) -> Mapping[str, str]:
        # Read-only view of the runner's session status book.
        return self.app.state.native_pane_status

    @property
    def pane_ref(self) -> PaneRef:
        return PaneRef(
            self.conv_id,
            terminal_resource_id(self.terminal_name, "main"),
            self.terminal_name,
            Path("/nonexistent/omnigent-test/tmux.sock"),
        )

    def write_claude_status(self, raw_status: str) -> None:
        """Rewrite Claude's status file the way Claude does: new content, new mtime."""
        self._mtime += 1.0
        record = {
            "pid": _PANE_PID,
            "sessionId": _CLAUDE_SESSION_UUID,
            "cwd": "/work",
            "kind": "interactive",
            "status": raw_status,
            "statusUpdatedAt": int(self._mtime * 1000),
        }
        self.status_file.write_text(json.dumps(record), encoding="utf-8")
        os.utime(self.status_file, (self._mtime, self._mtime))

    def pane_printed(self) -> None:
        """The pane just emitted output (tmux stamps ``window_activity``)."""
        self.output_clock["at"] = time.time()

    def pane_silent_for(self, seconds: float) -> None:
        """Advance time: the pane has printed nothing for *seconds*."""
        self.output_clock["at"] = time.time() - seconds

    async def _fire(self, name: str) -> None:
        """Invoke a captured watcher callback on a worker thread, like the daemon."""
        callback = self.callbacks[name]
        await asyncio.to_thread(callback)
        # ``_publish_status`` hops to the loop with ``call_soon_threadsafe``.
        for _ in range(3):
            await asyncio.sleep(0)

    async def tick(self) -> None:
        """One watcher poll: drives the real ``SessionStatusPoller.tick``."""
        await self._fire("on_tick")

    async def pane_activity(self) -> None:
        await self._fire("on_activity")

    async def pane_quiet(self) -> None:
        await self._fire("on_idle")

    async def relay_forwarder_status(self, status: str) -> None:
        """What the server does with a forwarder's ``external_session_status`` POST."""
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{self.conv_id}/events",
                json={"type": "external_session_status", "data": {"status": status}},
            )
        assert resp.status_code == 204, resp.text
        for _ in range(3):
            await asyncio.sleep(0)

    async def is_busy(self) -> bool:
        reaper = self.app.state.native_pane_reaper
        assert reaper is not None
        return await reaper._is_busy(self.pane_ref)

    async def reaper_scan(self) -> None:
        reaper = self.app.state.native_pane_reaper
        assert reaper is not None
        await reaper._scan_once()

    def pane_alive(self) -> bool:
        return self.terminal_registry.get(self.conv_id, self.terminal_name, "main") is not None

    def published_statuses(self) -> list[str]:
        """Drain and return the ``session.status`` values published for this session."""
        queue = _session_event_queues_ref.get(self.conv_id)
        out: list[str] = []
        if queue is None:
            return out
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict) and item.get("type") == "session.status":
                out.append(str(item.get("status")))
        return out

    def published_events(self) -> list[dict[str, Any]]:
        queue = _session_event_queues_ref.get(self.conv_id)
        out: list[dict[str, Any]] = []
        if queue is None:
            return out
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                out.append(item)
        return out


@asynccontextmanager
async def _native_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str = CLAUDE_NATIVE_TERMINAL_ROLE,
    terminal_name: str = "claude",
) -> AsyncIterator[_Pane]:
    """Build the real runner app and observe one native agent terminal in it."""
    # tmux probes the busy check falls through to. Bound by name when the app is
    # built, so stub first. No client is ever attached, and ``window_activity``
    # reports whatever the test's output clock says (the production evidence:
    # tmux silent, 0 clients). Neither can create a stale ``running``.
    output_clock = {"at": time.time() - 7200.0}
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda *_a: [])
    monkeypatch.setattr(
        native_cost_popup, "_tmux_window_activity_at", lambda *_a: output_clock["at"]
    )
    # Keep a stray approval-wait marker on this machine out of the busy check.
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    # The real poller resolves ``$CLAUDE_CONFIG_DIR/sessions/<pane_pid>.json``.
    config_dir = tmp_path / "claude-config"
    (config_dir / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    # Shrink the reaper window so two real scans can reap an idle pane.
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S", "0.001")

    conv_id = uuid.uuid4().hex
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance(terminal_name, "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[(terminal_name, "main")] = instance
    callbacks: dict[str, Callable[[], None]] = {}
    closed: list[str] = []

    # tmux fakes on the instance: pane pid, the watcher thread, and kill-server.
    instance.pane_pid_sync = lambda: _PANE_PID  # type: ignore[method-assign]

    def _capture_watcher(
        on_idle: Callable[[], None] | None = None,
        *,
        on_activity: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        on_tick: Callable[[], None] | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        for name, cb in (
            ("on_idle", on_idle),
            ("on_activity", on_activity),
            ("on_exit", on_exit),
            ("on_tick", on_tick),
        ):
            if cb is not None:
                callbacks[name] = cb

    async def _fake_close() -> None:
        closed.append(conv_id)
        instance.running = False

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    instance.close = _fake_close  # type: ignore[method-assign]

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    await app.state.session_resource_registry.observe_required_terminal(
        conv_id, terminal_name, "main", instance, resource_role=role
    )
    pane = _Pane(
        app=app,
        conv_id=conv_id,
        terminal_name=terminal_name,
        terminal_registry=terminal_registry,
        status_file=config_dir / "sessions" / f"{_PANE_PID}.json",
        callbacks=callbacks,
        output_clock=output_clock,
        closed_instances=closed,
    )
    try:
        yield pane
    finally:
        _session_event_queues_ref.pop(conv_id, None)


async def _claude_turn_then_relayed_idle_first(pane: _Pane) -> None:
    """A finished claude-native turn where the forwarder's ``Stop`` idle wins the race."""
    # Turn starts: Claude writes ``busy``; the real poller resolves the file by
    # pane pid and publishes ``running`` through ``_publish_status``.
    pane.write_claude_status("busy")
    pane.pane_printed()
    await pane.tick()
    assert pane.published_statuses() == ["running"]
    assert pane.mirror[pane.conv_id] == "running"
    assert await pane.is_busy() is True

    # Turn ends. The ``Stop`` hook -> forwarder -> server -> runner relay lands
    # first, through the runner's real events route.
    await pane.relay_forwarder_status("idle")
    # Then Claude flips its file to ``idle`` and the next watcher poll reads it.
    pane.write_claude_status("idle")
    await pane.tick()
    # Hours pass; the finished pane prints nothing and nobody attaches.
    pane.pane_silent_for(7200.0)


# ---------------------------------------------------------------------------
# Scenario 1: relayed Stop idle BEFORE the status file's idle (claude-native).
# ---------------------------------------------------------------------------


async def test_relayed_idle_before_file_idle_keeps_one_idle_on_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _native_pane(tmp_path, monkeypatch) as pane:
        await _claude_turn_then_relayed_idle_first(pane)

        assert pane.published_statuses() == []
        assert pane.registry._server_delivery_baseline[pane.conv_id] == ("idle", None)
        assert pane.mirror[pane.conv_id] == "idle"
        assert not pane.registry.session_turn_is_active(pane.conv_id)
        assert await pane.is_busy() is False


async def test_relayed_idle_before_file_idle_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _native_pane(tmp_path, monkeypatch) as pane:
        await _claude_turn_then_relayed_idle_first(pane)
        # Correct: once the turn is over (relayed Stop idle + file idle) and the
        # pane has been silent for hours with no client, it is not busy - no
        # reconnect resync needed.
        assert await pane.is_busy() is False


async def test_relayed_idle_before_file_idle_mirror_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status the claude model-change route reads must follow too."""
    async with _native_pane(tmp_path, monkeypatch) as pane:
        await _claude_turn_then_relayed_idle_first(pane)
        # The registry already knows the turn is over...
        assert not pane.registry.session_turn_is_active(pane.conv_id)
        # ...so the runner's status must not still claim ``running``.
        assert pane.mirror.get(pane.conv_id) != "running"


# ---------------------------------------------------------------------------
# Scenario 2: the real reaper scan reaps that pane without a resync.
# ---------------------------------------------------------------------------


async def test_reaper_scan_after_relayed_idle_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _native_pane(tmp_path, monkeypatch) as pane:
        await _claude_turn_then_relayed_idle_first(pane)
        for _ in range(3):
            await pane.reaper_scan()
            await asyncio.sleep(0.01)
        # Correct: an idle, unattended, silent pane is reaped after the window.
        assert not pane.pane_alive()


# ---------------------------------------------------------------------------
# Control: file idle FIRST, relayed idle second.
# ---------------------------------------------------------------------------


async def test_control_file_idle_before_relayed_idle_is_not_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _native_pane(tmp_path, monkeypatch) as pane:
        pane.write_claude_status("busy")
        pane.pane_printed()
        await pane.tick()
        assert pane.published_statuses() == ["running"]
        assert await pane.is_busy() is True

        # Same two edges, opposite order: the poller's idle claims the edge and
        # publishes locally; the relayed idle then only re-syncs the baseline.
        pane.write_claude_status("idle")
        await pane.tick()
        await pane.relay_forwarder_status("idle")
        pane.pane_silent_for(7200.0)

        assert pane.published_statuses() == ["idle"]
        assert pane.mirror[pane.conv_id] == "idle"
        assert await pane.is_busy() is False
        for _ in range(3):
            await pane.reaper_scan()
            await asyncio.sleep(0.01)
        assert not pane.pane_alive()


# ---------------------------------------------------------------------------
# Scenario 3 (breadth): the same ordering on the PTY-status path, for every
# status-emitting role whose forwarder relays a turn-end ``idle``. For
# claude-native this is the "status file not resolved (yet)" fallback.
# kiro-native is omitted (its forwarder relays no status); kimi-native is
# omitted (declared exempt in the harness registry, never offered to the reaper).
# ---------------------------------------------------------------------------

_PTY_ROLES = [
    pytest.param(CLAUDE_NATIVE_TERMINAL_ROLE, "claude", id="claude-native-no-file"),
    pytest.param(PI_NATIVE_TERMINAL_ROLE, "pi", id="pi-native"),
    pytest.param(CURSOR_NATIVE_TERMINAL_ROLE, "cursor", id="cursor-native"),
    pytest.param(GOOSE_NATIVE_TERMINAL_ROLE, "goose", id="goose-native"),
    pytest.param(QWEN_NATIVE_TERMINAL_ROLE, "qwen", id="qwen-native"),
    pytest.param(HERMES_NATIVE_TERMINAL_ROLE, "hermes", id="hermes-native"),
]


async def _pty_turn_then_relayed_idle_first(pane: _Pane, role: str) -> None:
    assert role in _STATUS_EMITTING_TERMINAL_ROLES
    # Pane redraws as the turn runs -> PTY ``running`` edge (published locally).
    pane.pane_printed()
    await pane.pane_activity()
    assert pane.published_statuses() == ["running"]
    assert pane.mirror[pane.conv_id] == "running"
    # The forwarder's turn-end ``idle`` lands before ~1 s of pane quiescence...
    await pane.relay_forwarder_status("idle")
    # ...then the watcher's own quiescence ``idle`` fires once (edge-triggered).
    await pane.pane_quiet()
    # Hours pass; the finished pane prints nothing and nobody attaches.
    pane.pane_silent_for(7200.0)


@pytest.mark.parametrize(("role", "terminal_name"), _PTY_ROLES)
async def test_pty_relayed_idle_before_pane_quiet_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, terminal_name: str
) -> None:
    async with _native_pane(tmp_path, monkeypatch, role=role, terminal_name=terminal_name) as pane:
        await _pty_turn_then_relayed_idle_first(pane, role)
        assert pane.published_statuses() == []
        assert pane.mirror[pane.conv_id] == "idle"
        assert await pane.is_busy() is False
        for _ in range(3):
            await pane.reaper_scan()
            await asyncio.sleep(0.01)
        assert not pane.pane_alive()
