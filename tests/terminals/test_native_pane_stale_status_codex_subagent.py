"""A finished codex-native SUB-AGENT's pane must be idle-reaped.

A codex-native turn start publishes ``running``; the runner drops its own
turn-end ``idle`` (the forwarder owns idle), and the forwarder's ``idle``
reaches the runner as a relayed ``external_session_status``. The runner's
status book records that relayed edge, so the reaper sees the finished child as
idle and reaps its pane (and the codex app-server torn down with it) on the same
schedule as a pane that never ran a turn.

Everything below is driven through ``create_runner_app`` and its real HTTP
routes (session init, ``message``, ``external_session_status``) and the real
reaper object the app builds. The only fakes are the tmux / codex process layer
and the reaper's clock; each is marked ``FAKE:`` below.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult, TerminalInstance
from omnigent.native import native_cost_popup
from omnigent.runner import app as runner_app
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import (
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import pane_reaper as pane_reaper_module
from omnigent.terminals import registry as terminal_registry_module
from omnigent.terminals.pane_reaper import PaneRef
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

PARENT_ID = "conv_zz_codex_parent_orchestrator"
CHILD_ID = "conv_zz_codex_subagent_child"
# A second codex-native pane on the SAME runner that never ran a turn. It is
# the in-test control: the finished sub-agent must be reaped on its schedule.
CONTROL_ID = "conv_zz_codex_control_never_ran"
AGENT_ID = "ag_zz_codex_native_worker"
CODEX_TERMINAL_ID = terminal_resource_id("codex", "main")

_OUR_IDS = (PARENT_ID, CHILD_ID, CONTROL_ID)


# ── fakes of the tmux / codex process layer (NOT the logic under test) ───────


class _FakeTmuxPane(TerminalInstance):
    """A codex TUI pane with no real tmux server behind it.

    FAKE: replaces the tmux subprocess layer only. It never publishes a
    ``session.status`` event, so it cannot create or mask a stale status.

    * ``launch`` / ``is_alive`` / ``close``: no tmux; liveness follows ``running``.
    * ``start_idle_watcher_thread``: records the callbacks the runner wires
      instead of polling ``tmux capture-pane`` on a daemon thread. For the
      codex-native role the runner wires NO ``on_idle`` (the role is not in
      ``_STATUS_EMITTING_TERMINAL_ROLES``), which the tests assert.
    """

    watcher_kwargs: dict[str, Any] | None = None
    closed: bool = False

    async def launch(self, *, cwd: Path | None = None) -> None:
        del cwd
        self.running = True

    async def is_alive(self) -> bool:
        return self.running

    async def close(self) -> None:
        self.closed = True
        self.running = False

    async def set_conversation_link(self, conversation_link: str | None) -> None:
        self.conversation_link = conversation_link

    def start_idle_watcher_thread(  # type: ignore[override]
        self,
        on_idle: Any = None,
        **kwargs: Any,
    ) -> None:
        self.watcher_kwargs = {"on_idle": on_idle, **kwargs}


class _TmuxProbes:
    """Records every tmux probe the reaper busy check makes.

    FAKE: stands in for ``_list_tmux_clients`` / ``_tmux_window_activity_at``
    (``tmux list-clients`` / ``display -p #{window_activity}``). Models the
    production evidence: 0 attached clients, pane silent for hours. It only
    answers "not busy", so it can only make the reaper MORE willing to reap;
    it cannot cause a pane to be spared.
    """

    def __init__(self) -> None:
        self.client_probes: list[str] = []
        self.activity_probes: list[str] = []

    def list_clients(self, socket_path: str, *_args: Any) -> list[str]:
        self.client_probes.append(socket_path)
        return []

    def window_activity_at(self, socket_path: str, *_args: Any) -> float:
        self.activity_probes.append(socket_path)
        return time.time() - 4 * 3600  # last output ~4 h ago

    def probed(self, socket_path: Path) -> bool:
        return str(socket_path) in self.client_probes


@dataclasses.dataclass
class _Rig:
    app: Any
    registry: TerminalRegistry
    resources: SessionResourceRegistry
    pm: _FakeProcessManager
    probes: _TmuxProbes
    reaper: Any

    def pane(self, conv_id: str) -> _FakeTmuxPane:
        inst = self.registry.get(conv_id, "codex", "main")
        assert isinstance(inst, _FakeTmuxPane), f"no codex pane registered for {conv_id}"
        return inst

    def pane_ref(self, conv_id: str) -> PaneRef:
        refs = [p for p in self.reaper._list_native_panes() if p.conversation_id == conv_id]
        assert len(refs) == 1, f"reaper does not list a codex pane for {conv_id}: {refs}"
        return refs[0]


@pytest.fixture
def _clean_runner_module_state() -> Iterator[None]:
    """Snapshot/restore the process-wide maps ``omnigent.runner.app`` keeps.

    Mirrors ``tests/runner/test_native_subagent_inbox_delivery.py``'s fixture:
    the sub-agent work registry, inbox and event queues are module-level dicts
    that would otherwise leak between tests.
    """
    saved_by_child = dict(runner_app._subagent_work_by_child)
    saved_by_parent = {k: set(v) for k, v in runner_app._subagent_work_by_parent.items()}
    saved_drained = set(runner_app._drained_delivered_subagent_children)
    saved_recovery_done = set(runner_app._subagent_recovery_done)
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved_by_child)
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved_by_parent)
        runner_app._drained_delivered_subagent_children.clear()
        runner_app._drained_delivered_subagent_children.update(saved_drained)
        runner_app._subagent_recovery_done.clear()
        runner_app._subagent_recovery_done.update(saved_recovery_done)
        for conv in _OUR_IDS:
            runner_app._session_inboxes_ref.pop(conv, None)
            runner_app._session_event_queues_ref.pop(conv, None)
            runner_app._child_session_parents.pop(conv, None)
            runner_app._subagent_recovery_locks.pop(conv, None)


def _codex_native_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="codex-worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )


def _build_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    """Build a real runner app whose only fakes are the tmux / codex process layer."""
    monkeypatch.delenv("OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S", raising=False)  # 1 h default

    # FAKE (tmux probes): bound by name when the app is built, so patch first.
    probes = _TmuxProbes()
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", probes.list_clients)
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", probes.window_activity_at)
    # Isolation only: keep bridge files / approval markers inside tmp_path.
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")

    # FAKE (tmux spawn): the real TerminalRegistry.launch runs, but the
    # instance it creates is a _FakeTmuxPane instead of a tmux server.
    counter = {"n": 0}

    def _fake_create_terminal_instance(
        name: str, session_key: str, spec: Any, **_kwargs: Any
    ) -> TerminalCreateResult:
        del spec
        counter["n"] += 1
        pane_dir = tmp_path / f"pane-{counter['n']}-{name}-{session_key}"
        pane_dir.mkdir(parents=True, exist_ok=True)
        return TerminalCreateResult(
            instance=_FakeTmuxPane(
                name=name,
                session_key=session_key,
                socket_path=pane_dir / "tmux.sock",
                private_dir=pane_dir,
            ),
            cwd=tmp_path,
        )

    monkeypatch.setattr(
        terminal_registry_module, "create_terminal_instance", _fake_create_terminal_instance
    )

    # FAKE (codex app-server + bridge bring-up): session init's
    # ``_launch_native_terminal("codex-native", ctx, ...)`` normally spawns
    # ``codex app-server`` and then launches the TUI pane. The fake performs
    # only the pane-registration tail of ``_auto_create_codex_terminal``:
    # the REAL ``launch_auxiliary_terminal`` with
    # ``resource_role=CODEX_NATIVE_TERMINAL_ROLE``. It publishes no
    # ``session.status``.
    async def _fake_launch_native_terminal(harness_name: str, ctx: Any, **_kw: Any) -> bool:
        assert harness_name == "codex-native", harness_name
        await ctx.resource_registry.launch_auxiliary_terminal(
            session_id=ctx.session_id,
            terminal_name="codex",
            session_key="main",
            resource_role=CODEX_NATIVE_TERMINAL_ROLE,
            spec=TerminalEnvSpec(
                os_env=OSEnvSpec(type="caller_process", cwd=str(tmp_path)),
                command="codex",
                args=["--remote", "ws://127.0.0.1:0"],
            ),
        )
        return True

    monkeypatch.setattr(runner_app, "_launch_native_terminal", _fake_launch_native_terminal)
    # FAKE (provider env for the codex app-server spawn): same stub as
    # tests/runner/test_native_subagent_inbox_delivery.py.
    monkeypatch.setattr(runner_app, "_resolve_native_spawn_env", AsyncMock(return_value={}))

    # FAKE (harness subprocess): codex-native's ``run_turn`` pastes the prompt
    # into the app-server and returns at once; the scripted stream models that
    # (created -> completed). The runner's own stream-end path then runs for real.
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_codex_child"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_codex_child"}}),
        ]
    )
    pm = _FakeProcessManager(harness)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _codex_native_spec()

    registry = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=registry)
    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=resources,
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    reaper = app.state.native_pane_reaper
    assert reaper is not None, "runner did not wire the native pane reaper"
    return _Rig(
        app=app, registry=registry, resources=resources, pm=pm, probes=probes, reaper=reaper
    )


def _drain_status_events(conv_id: str) -> list[str]:
    queue = runner_app._session_event_queues_ref.get(conv_id)
    statuses: list[str] = []
    if queue is None:
        return statuses
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, dict) and item.get("type") == "session.status":
            statuses.append(str(item.get("status")))
    return statuses


async def _drive_codex_subagent_to_relayed_idle(rig: _Rig) -> dict[str, Any]:
    """Run one full codex-native sub-agent turn the way production does.

    1. Session init (``POST /v1/sessions``) for the child and a control session;
       each registers its ``codex``/``main`` pane with the codex-native role.
    2. Spawn bookkeeping exactly as ``tool_dispatch`` does for a sub-agent send:
       ``register_child_session`` + ``register_subagent_work`` and a parent inbox.
    3. ``POST /v1/sessions/{child}/events`` ``type=message`` -> the runner starts
       the turn, publishes ``running``, streams the harness, runs its own
       stream-end status path.
    4. ``POST /v1/sessions/{child}/events`` ``type=external_session_status``
       ``running`` then ``idle`` -> the server's relay of the codex forwarder's
       turn/started and turn/completed edges.
    """
    observed: dict[str, Any] = {}
    async with _runner_client(rig.app) as client:
        for conv in (CHILD_ID, CONTROL_ID):
            init = await client.post(
                "/v1/sessions", json={"session_id": conv, "agent_id": AGENT_ID}
            )
            assert init.status_code == 201, init.text

        # Parent-side spawn bookkeeping (tool_dispatch._execute_subagent_tool).
        runner_app._session_inboxes_ref[PARENT_ID] = asyncio.Queue()
        runner_app.register_child_session(
            CHILD_ID,
            parent_session_id=PARENT_ID,
            title="codex-worker:task",
            tool="codex-worker",
            session_name="task",
        )
        work = runner_app.register_subagent_work(
            parent_session_id=PARENT_ID,
            child_session_id=CHILD_ID,
            agent="codex-worker",
            title="task",
        )
        work.status = "running"

        _drain_status_events(CHILD_ID)  # isolate the turn's own status edges

        msg = await client.post(
            f"/v1/sessions/{CHILD_ID}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": AGENT_ID,
                "content": [{"type": "input_text", "text": "do the task"}],
            },
        )
        assert msg.status_code == 202, msg.text
        assert msg.json()["status"] == "accepted", msg.text
        observed["turn_active_mid_turn"] = rig.resources.session_turn_is_active(CHILD_ID)

        # Let the runner's background turn finish through its real stream-end path.
        for _ in range(500):
            if CHILD_ID not in rig.app.state.active_turns and not rig.pm.has_active_turn(CHILD_ID):
                break
            await asyncio.sleep(0.01)
        assert CHILD_ID not in rig.app.state.active_turns, "runner turn never finished"
        assert rig.pm.has_active_turn(CHILD_ID) is False
        observed["runner_status_edges"] = _drain_status_events(CHILD_ID)

        # The codex forwarder's turn/started edge, relayed by the server
        # (routes_events.py forwards every external_session_status to the runner).
        relay_running = await client.post(
            f"/v1/sessions/{CHILD_ID}/events",
            json={"type": "external_session_status", "data": {"status": "running"}},
        )
        observed["relay_running_status_code"] = relay_running.status_code

        # The codex forwarder's turn/completed edge, relayed the same way.
        relay = await client.post(
            f"/v1/sessions/{CHILD_ID}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "task done"},
            },
        )
        observed["relay_status_code"] = relay.status_code
        observed["runner_status_edges_after_relay"] = _drain_status_events(CHILD_ID)

    inbox = runner_app._session_inboxes_ref[PARENT_ID]
    delivered: list[dict[str, Any]] = []
    while not inbox.empty():
        delivered.append(inbox.get_nowait())
    observed["parent_inbox"] = delivered
    return observed


# ── Scenario 1: the reaper busy check right after the relayed idle ──────────


async def test_codex_subagent_relayed_idle_is_recorded_without_a_wire_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _clean_runner_module_state: None,
) -> None:
    """The relayed idle ends the child's status; the runner still publishes no idle."""
    rig = _build_rig(tmp_path, monkeypatch)
    observed = await _drive_codex_subagent_to_relayed_idle(rig)

    assert rig.resources.terminal_resource_role(CHILD_ID, CODEX_TERMINAL_ID) == (
        CODEX_NATIVE_TERMINAL_ROLE
    )
    child_ref = rig.pane_ref(CHILD_ID)
    assert rig.pane(CHILD_ID).watcher_kwargs is not None
    assert rig.pane(CHILD_ID).watcher_kwargs["on_idle"] is None

    assert observed["turn_active_mid_turn"] is True
    assert (CHILD_ID, "resp_codex_child") in rig.pm.marked_in_flight
    assert CHILD_ID in rig.pm.cleared_in_flight
    assert observed["runner_status_edges"] == ["running"]
    assert observed["relay_running_status_code"] == 204
    assert observed["relay_status_code"] == 204
    assert [(p["conversation_id"], p["status"]) for p in observed["parent_inbox"]] == [
        (CHILD_ID, "completed")
    ]
    assert observed["runner_status_edges_after_relay"] == []

    assert rig.resources.session_turn_is_active(CHILD_ID) is False
    assert rig.app.state.native_pane_status.get(CHILD_ID) == "idle"
    assert await rig.reaper._is_busy(child_ref) is False
    assert rig.probes.probed(child_ref.socket_path)


async def test_codex_subagent_busy_check_after_relayed_idle_expected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _clean_runner_module_state: None,
) -> None:
    """Once the forwarder's idle is relayed, a silent, unattended pane is NOT busy."""
    rig = _build_rig(tmp_path, monkeypatch)
    await _drive_codex_subagent_to_relayed_idle(rig)
    child_ref = rig.pane_ref(CHILD_ID)

    assert rig.resources.session_turn_is_active(CHILD_ID) is False
    assert await rig.reaper._is_busy(child_ref) is False


# ── Scenario 2: the real reaper scan across > 1 idle window ─────────────────


class _FakeMonotonic:
    """Injected clock for ``pane_reaper`` only (the event loop keeps real time).

    FAKE: time source. It advances both panes identically, so it cannot make
    one pane look busier than the other.
    """

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def monotonic(self) -> float:
        return self.now


async def _scan_for(rig: _Rig, clock: _FakeMonotonic, *, seconds: float) -> dict[str, float]:
    """Run the production ``_scan_once`` every 60 s of simulated time.

    :returns: ``{conv_id: simulated seconds after the first scan when reaped}``.
    """
    start = clock.now
    reaped_at: dict[str, float] = {}
    interval = rig.reaper._reaper_interval_s
    while clock.now - start <= seconds:
        await rig.reaper._scan_once()
        for conv in (CHILD_ID, CONTROL_ID):
            if conv not in reaped_at and rig.registry.get(conv, "codex", "main") is None:
                reaped_at[conv] = clock.now - start
        clock.now += interval
    return reaped_at


async def test_codex_subagent_reaper_scan_over_three_hours_expected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _clean_runner_module_state: None,
) -> None:
    """The finished child is reaped on the same schedule as the never-used control."""
    rig = _build_rig(tmp_path, monkeypatch)
    await _drive_codex_subagent_to_relayed_idle(rig)
    child_pane = rig.pane(CHILD_ID)
    control_pane = rig.pane(CONTROL_ID)

    assert rig.reaper._idle_timeout_s == 3600.0
    assert rig.reaper._reaper_interval_s == 60.0
    clock = _FakeMonotonic()
    monkeypatch.setattr(pane_reaper_module, "time", SimpleNamespace(monotonic=clock.monotonic))

    reaped_at = await _scan_for(rig, clock, seconds=3 * 3600)

    assert reaped_at.get(CHILD_ID) == 3600.0
    assert reaped_at.get(CONTROL_ID) == 3600.0
    assert child_pane.closed is True
    assert control_pane.closed is True
    assert rig.resources.terminal_resource_role(CHILD_ID, CODEX_TERMINAL_ID) is None
