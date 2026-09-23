"""Shared rig for native-pane reaper tests: a real runner app, fakes at the edges.

The runner is built with ``create_runner_app`` and one native pane is observed
through the real ``SessionResourceRegistry`` with the harness's real role from
the production harness registry. Only the tmux probes, the pane's watcher
thread, the pane close and the Omnigent server client are faked.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harness_plugins import NativeCodingAgent, native_agents
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.native import native_cost_popup
from omnigent.runner.app import _session_event_queues_ref, create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.session_status import SessionStatusBook
from omnigent.terminals.pane_reaper import NativePaneReaper, PaneAssessment, PaneRef
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import make_test_terminal_instance

# Fake pane pid: the real claude status poller resolves
# ``$CLAUDE_CONFIG_DIR/sessions/<pid>.json`` from ``#{pane_pid}``.
CLAUDE_PANE_PID = 4178604


def native_agent(key: str) -> NativeCodingAgent:
    """The production registry row for native agent *key*, e.g. ``"codex"``."""
    for agent in native_agents():
        if agent.key == key:
            return agent
    raise KeyError(key)


class TmuxFakes:
    """``_list_tmux_clients`` / ``_tmux_window_activity_at`` doubles.

    :param output_age_s: Seconds since the pane last printed, or ``None`` when
        tmux cannot answer.
    :param clock: Fake monotonic clock; once :meth:`printed` is called the
        output age is measured on it instead of *output_age_s*.
    """

    def __init__(
        self, output_age_s: float | None = 7200.0, *, clock: Callable[[], float] | None = None
    ) -> None:
        self.output_age_s = output_age_s
        self.clients: list[str] = []
        self.calls: list[str] = []
        self._clock = clock
        self._printed_at: float | None = None

    def printed(self) -> None:
        """The pane emitted output now (tmux stamps ``window_activity``)."""
        assert self._clock is not None, "printed() needs a clock"
        self._printed_at = self._clock()

    def list_clients(self, *_args: Any) -> list[str]:
        self.calls.append("list_clients")
        return list(self.clients)

    def window_activity_at(self, *_args: Any) -> float | None:
        self.calls.append("window_activity")
        if self._printed_at is not None and self._clock is not None:
            return time.time() - max(0.0, self._clock() - self._printed_at)
        if self.output_age_s is None:
            return None
        return time.time() - self.output_age_s


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@dataclass
class FakeServerClient:
    """Omnigent server double answering the session snapshot and labels.

    :param pending: ``pending_elicitations`` in the session snapshot.
    :param status: ``status`` in the session snapshot.
    :param status_code: HTTP status of the session snapshot.
    :param error: Raised by every session-snapshot GET when set.
    """

    pending: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    status_code: int = 200
    error: Exception | None = None
    labels: dict[str, str] = field(default_factory=dict)
    snapshot_gets: int = 0

    async def get(self, url: str, **_kwargs: Any) -> _Response:
        if url.endswith("/labels"):
            return _Response(200, {"labels": dict(self.labels)})
        self.snapshot_gets += 1
        if self.error is not None:
            raise self.error
        return _Response(
            self.status_code, {"pending_elicitations": self.pending, "status": self.status}
        )

    async def post(self, url: str, **_kwargs: Any) -> _Response:
        del url
        return _Response(200, {})

    async def patch(self, url: str, **_kwargs: Any) -> _Response:
        del url
        return _Response(200, {})


@dataclass
class PaneRig:
    """One runner app with one observed native pane."""

    app: Any
    conv_id: str
    agent: NativeCodingAgent
    terminal_registry: TerminalRegistry
    resources: SessionResourceRegistry
    tmux: TmuxFakes
    server: FakeServerClient
    callbacks: dict[str, Callable[[], None]]
    closed: list[str]

    @property
    def book(self) -> SessionStatusBook:
        return self.app.state.session_status_book

    @property
    def reaper(self) -> NativePaneReaper:
        reaper = self.app.state.native_pane_reaper
        assert reaper is not None
        return reaper

    @property
    def pane(self) -> PaneRef:
        instance = self.terminal_registry.get(self.conv_id, self.agent.terminal_name, "main")
        assert instance is not None
        return PaneRef(
            self.conv_id,
            terminal_resource_id(self.agent.terminal_name, "main"),
            self.agent.terminal_name,
            instance.socket_path,
        )

    def listed(self) -> bool:
        return any(p.conversation_id == self.conv_id for p in self.reaper._list_native_panes())

    def alive(self) -> bool:
        return (
            self.terminal_registry.get(self.conv_id, self.agent.terminal_name, "main") is not None
        )

    async def assess(self) -> PaneAssessment:
        return await self.reaper.assess(self.pane)

    async def is_busy(self) -> bool:
        return await self.reaper._is_busy(self.pane)

    def drain(self) -> None:
        _session_event_queues_ref.pop(self.conv_id, None)

    @property
    def claude_status_file(self) -> Path:
        """Where the real claude status poller finds this pane's Claude."""
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions" / f"{CLAUDE_PANE_PID}.json"

    def write_claude_status(self, raw_status: str, *, waiting_for: str | None = None) -> None:
        """Rewrite Claude's ``sessions/<pid>.json`` the way Claude does."""
        self._claude_mtime = getattr(self, "_claude_mtime", time.time() - 1000.0) + 1.0
        record: dict[str, object] = {
            "pid": CLAUDE_PANE_PID,
            "sessionId": "0d5c8f3e-7a51-4c7b-9d62-3f1e2a9b8c10",
            "kind": "interactive",
            "status": raw_status,
            "statusUpdatedAt": int(self._claude_mtime * 1000),
        }
        if waiting_for is not None:
            record["waitingFor"] = waiting_for
        path = self.claude_status_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        os.utime(path, (self._claude_mtime, self._claude_mtime))

    async def fire(self, name: str) -> None:
        """Invoke a captured watcher callback on a worker thread, like the daemon."""
        await asyncio.to_thread(self.callbacks[name])
        # Registry publishers hop to the loop with ``call_soon_threadsafe``.
        for _ in range(3):
            await asyncio.sleep(0)


async def build_pane_rig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = "codex",
    idle_timeout_s: float = 3600.0,
    server: FakeServerClient | None = None,
    tmux: TmuxFakes | None = None,
    process_manager: Any = None,
    status_clock: Callable[[], float] | None = None,
    spec_resolver: Any = None,
) -> PaneRig:
    """Build a runner app and observe one native pane for harness *key*."""
    tmux = tmux or TmuxFakes()
    server = server or FakeServerClient()
    for agent_row in native_agents():
        bridge = importlib.import_module(f"omnigent.harnesses.{agent_row.key}_native.bridge")
        if hasattr(bridge, "_BRIDGE_ROOT"):
            monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / f"{agent_row.key}-bridge")
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", tmux.list_clients)
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", tmux.window_activity_at)
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    # The claude bridge validates its tree from the temp dir down; anchor it
    # at tmp_path so a real relay can start under the patched root.
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S", str(idle_timeout_s))
    agent = native_agent(key)
    conv_id = f"conv_{uuid.uuid4().hex[:12]}"
    terminal_registry = TerminalRegistry()
    resources = SessionResourceRegistry(
        terminal_registry=terminal_registry, status_clock=status_clock
    )
    if process_manager is None:
        from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

        process_manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=process_manager,
        server_client=server,  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=resources,
        spec_resolver=spec_resolver,
    )
    instance = make_test_terminal_instance(agent.terminal_name, "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[(agent.terminal_name, "main")] = (
        instance
    )
    callbacks: dict[str, Callable[[], None]] = {}
    closed: list[str] = []

    def _capture_watcher(on_idle: Callable[[], None] | None = None, **kwargs: Any) -> None:
        for name, callback in (("on_idle", on_idle), *kwargs.items()):
            if callable(callback):
                callbacks[name] = callback

    async def _close() -> None:
        closed.append(conv_id)
        instance.running = False

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    instance.close = _close  # type: ignore[method-assign]
    instance.pane_pid_sync = lambda: CLAUDE_PANE_PID  # type: ignore[method-assign]
    await resources.observe_auxiliary_terminal(
        conv_id, agent.terminal_name, "main", instance, resource_role=agent.harness
    )
    return PaneRig(
        app=app,
        conv_id=conv_id,
        agent=agent,
        terminal_registry=terminal_registry,
        resources=resources,
        tmux=tmux,
        server=server,
        callbacks=callbacks,
        closed=closed,
    )


class _FakeServerProcess:
    """A codex app-server / opencode serve stand-in that records its close."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeRelay:
    """A tool/comment relay stand-in that records its close."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _TrackedRelay:
    """Records the close of a real relay the runner started for a turn."""

    def __init__(self, relay: Any) -> None:
        self.relay = relay
        self.closed = False
        real_close = relay.close

        def _close() -> None:
            self.closed = True
            real_close()

        relay.close = _close

    def close(self) -> None:
        self.relay.close()


@dataclass
class PlantedSidecars:
    """The per-session sidecars a harness's native pane runs beside, faked.

    Only the vendor server the harness really runs is planted (a codex
    app-server for codex, ``opencode serve`` for opencode): each vendor server's
    teardown also cancels the forwarder, so planting one for every harness
    would hide a teardown that forgets the forwarder itself.
    """

    session_id: str
    forwarder: asyncio.Task[object]
    codex_app_server: _FakeServerProcess | None
    opencode_server: _FakeServerProcess | None
    relay: _FakeRelay | _TrackedRelay
    prompt_waiter: asyncio.Task[None]
    relays: dict[str, Any] = field(default_factory=dict)

    @property
    def planted(self) -> list[str]:
        """Names of what was planted, in :meth:`leftovers` order."""
        names = ["forwarder"]
        if self.codex_app_server is not None:
            names.append("codex_app_server")
        if self.opencode_server is not None:
            names.append("opencode_server")
        return [*names, "comment_relay", "claude_prompt_waiter"]

    def leftovers(self, app: Any) -> list[str]:
        """Names of the sidecars still registered or running."""
        from omnigent.runner.native import orchestration

        sid = self.session_id
        codex, opencode = self.codex_app_server, self.opencode_server
        left = []
        if sid in orchestration._AUTO_FORWARDER_TASKS or not self.forwarder.done():
            left.append("forwarder")
        if sid in orchestration._AUTO_CODEX_APP_SERVERS or (codex and not codex.closed):
            left.append("codex_app_server")
        if sid in orchestration._AUTO_OPENCODE_SERVERS or (opencode and not opencode.closed):
            left.append("opencode_server")
        if sid in app.state.session_comment_relays or not self.relay.closed:
            left.append("comment_relay")
        if sid in app.state.claude_prompt_waiters or not self.prompt_waiter.done():
            left.append("claude_prompt_waiter")
        return left

    def intact(self, app: Any) -> bool:
        return self.leftovers(app) == self.planted

    def discard(self) -> None:
        """Drop whatever the test left registered (test hygiene)."""
        from omnigent.runner.native import orchestration

        orchestration._AUTO_FORWARDER_TASKS.pop(self.session_id, None)
        orchestration._AUTO_CODEX_APP_SERVERS.pop(self.session_id, None)
        orchestration._AUTO_OPENCODE_SERVERS.pop(self.session_id, None)
        self.forwarder.cancel()
        self.prompt_waiter.cancel()
        binding = self.relays.get(self.session_id)
        if binding is not None and getattr(binding, "relay", None) is self._relay_object():
            del self.relays[self.session_id]
        if not self.relay.closed:
            self.relay.close()

    def _relay_object(self) -> object:
        return self.relay.relay if isinstance(self.relay, _TrackedRelay) else self.relay


def plant_sidecars(
    app: Any, session_id: str, bridge_dir: Path, *, harness_key: str
) -> PlantedSidecars:
    """Register the sidecars *harness_key*'s native pane runs for *session_id*.

    Every harness gets a forwarder task, a tool/comment relay and a claude
    prompt waiter; only codex gets a codex app-server and only opencode an
    ``opencode serve``. A relay the runner already started for the session
    (the per-turn ensure does for claude, codex and antigravity) is kept and
    tracked instead of replaced.
    """
    from omnigent.runner.app import _CommentRelayBinding
    from omnigent.runner.native import orchestration

    forwarder: asyncio.Task[object] = asyncio.create_task(asyncio.sleep(3600))
    orchestration._register_auto_forwarder_task(session_id, forwarder)
    codex_app_server = _FakeServerProcess() if harness_key == "codex" else None
    opencode_server = _FakeServerProcess() if harness_key == "opencode" else None
    if codex_app_server is not None:
        orchestration._AUTO_CODEX_APP_SERVERS[session_id] = codex_app_server  # type: ignore[assignment]
    if opencode_server is not None:
        orchestration._AUTO_OPENCODE_SERVERS[session_id] = opencode_server  # type: ignore[assignment]
    relays = app.state.session_comment_relays
    existing = relays.get(session_id)
    relay: _FakeRelay | _TrackedRelay
    if existing is not None:
        relay = _TrackedRelay(existing.relay)
    else:
        relay = _FakeRelay()
        relays[session_id] = _CommentRelayBinding(
            relay=relay,  # type: ignore[arg-type]
            spec_entry=None,
            bridge_dir=bridge_dir,
        )
    prompt_waiter: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(3600))
    app.state.claude_prompt_waiters[session_id] = prompt_waiter
    return PlantedSidecars(
        session_id=session_id,
        forwarder=forwarder,
        codex_app_server=codex_app_server,
        opencode_server=opencode_server,
        relay=relay,
        prompt_waiter=prompt_waiter,
        relays=relays,
    )
